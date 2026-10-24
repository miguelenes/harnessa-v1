# Recovery and restore guide

## Restore into a fresh Compose installation

Restore is deliberately fail-closed: it accepts only a complete checksum-verified bundle, a database with zero public tables, and an empty artifacts volume. It never runs `pg_restore --clean` against an existing database.

1. Provision the same deployment secrets separately from the encrypted backup. Keep the restored secret values outside the backup bundle.
2. Select a fresh Compose project name and fresh database/artifact volumes. The restore script starts PostgreSQL and Redis, waits for readiness, and checks the database and artifact volume before changing either.
3. Set the target database variables for the new instance and run:

```bash
scripts/harnessa-restore.sh /secure/backup/harnessa-YYYYMMDDTHHMMSSZ
scripts/harnessa-doctor.sh
```

4. Re-register external secret references and restore the actual secret material from the secret manager. Rotate bootstrap credentials. Re-establish GitHub App configuration and installation grants; backups intentionally do not contain private keys or webhook secrets.
5. Compare restored repository revisions, task/run IDs, artifact metadata and SHA-256 bytes to the backup manifest. Reconcile external GitHub effects before resuming workers. Start dispatch only after the operator verifies that no effect could be duplicated.

The restore script validates checksums and requires an empty target database. It restores artifacts only into an empty named volume. It does not provision secrets, GitHub installations, provider accounts, repository grants or model-route authorization. Keep the original backup intact until the new deployment has passed those checks.

## Common incidents

### Audited recovery commands

The command-line client prompts for the Harnessa password without echoing it. It keeps the authenticated cookie jar in memory for that invocation, never places a password or cookie in process arguments, and prints only identifiers and statuses. Use the API and web origins configured for this deployment; production origins must use HTTPS. Loopback HTTP is accepted only for local development.

List a project's dead letters (the API requires an administrator or operator membership):

```bash
uv run --project apps/api python apps/api/harnessa/recovery_cli.py \
  --api-origin https://harnessa.example.com \
  --web-origin https://app.harnessa.example.com \
  dead-letters --project-id 00000000-0000-0000-0000-000000000000
```

Before replaying one, inspect the task, run, dead-letter reason, and any external effect in the authenticated UI. Resolve ambiguous GitHub/provider effects first. The replay command requires the exact dead-letter UUID and a reason; Harnessa checks the project role and failed-task state, writes an idempotent task transition and event, and records the operator actor.

```bash
uv run --project apps/api python apps/api/harnessa/recovery_cli.py \
  --api-origin https://harnessa.example.com \
  --web-origin https://app.harnessa.example.com \
  replay-dead-letter --dead-letter-id 00000000-0000-0000-0000-000000000000
```

Retry only a specific failed or ignored GitHub delivery after checking the delivery in the GitHub inspector. The command requires the exact delivery UUID and reason; the endpoint requires an organization administrator and appends an actor/reason/time repair record before requeueing the durable delivery intent.

```bash
uv run --project apps/api python apps/api/harnessa/recovery_cli.py \
  --api-origin https://harnessa.example.com \
  --web-origin https://app.harnessa.example.com \
  retry-github-delivery --delivery-id 00000000-0000-0000-0000-000000000000
```

These commands do not bypass authorization, reset budgets, or declare an external effect successful. Review the resulting task, delivery, and audit state before dispatch resumes.

### API is not ready

Run `scripts/harnessa-doctor.sh`; inspect `podman compose --project-name harnessa -f infra/compose.yaml logs --tail=200 api db redis litellm`. Check migration output and database connectivity. Do not repeatedly recreate volumes to clear readiness failures.

### Worker stopped or stale run lease

Check the worker logs and persisted run/experiment heartbeat. Restarting the worker is safe for fenced runs; stale outputs cannot publish. Verify task/run state after restart and inspect dead letters before replay. Replay only a specific reviewed dead-letter ID with an operator reason and fresh idempotency key after reconciling any ambiguous external effect.

### GitHub delivery or effect is uncertain

Use the GitHub inspector to review webhook delivery and effect state. A webhook receipt is not proof of current PR truth. Refresh the remote PR/head/check state through the adapter and reconcile the stable effect key before a retry. Never repeat issue/PR/merge writes manually based only on a timeout.

### Unknown provider spend

Unknown usage stays explicitly unknown and reservations are reconciled conservatively. Inspect the project request ledger and provider dashboard. Do not free a reservation or repeat an operation until its upstream request is resolved.

### Disk pressure

Check database, Redis, artifacts and runner workspace volumes separately. Take a verified backup before pruning. Harnessa does not yet ship an automated artifact retention/garbage-collection command; do not remove content-addressed files by hand while database references remain.
