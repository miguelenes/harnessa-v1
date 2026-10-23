# Harness² integration

## Compatibility pin

Harnessa pins Google Research Harness² at commit `1cfe254e8d25b15f37048c3730134686abe60335` and the upstream `Apache-2.0` license. The Python package is `harness2` version `0.1.0`, supports Python 3.12–3.13, and has no core Python runtime dependencies. The pin is recorded in `pyproject.toml` and `uv.lock`.

The adapter uses upstream `harness2.core.tree` validation/diff helpers, `harness2.core.schedule.run_task`, `harness2.core.context` leakage checks and the OpenCode substrate surface. Harnessa maps the eight upstream categories—system prompt, guardrails, memory, tool guidance, subagents, skills, scripts and plugins—to its product paths in `harnessa.adapters.harness2`. This is an adapter contract, not a claim that Harnessa's historical attachments are an upstream schema.

## Product policy boundary

Candidate content is limited to validated OpenCode guidance paths and bounded to 500 files / 2 MB. Installation policy, sandbox permissions, evaluator configuration, hidden answers, grading rubrics and promotion policy remain host-owned. The names `harnessa_policy.py`, `host_policy.json`, `evaluator_policy.json`, `promotion_policy.json` and `sandbox_policy.py` are rejected. Candidate edits are applied only under a resolved per-candidate `harness/` directory. Holdout-shaped files and grader verdicts are rejected before context is sent to the probe router. Model-backed probe requests use Harnessa's probe pool, task/project budget reservation and idempotency ledger; direct upstream provider CLIs are not used.

Project APIs expose manifest and probe metadata, persist bounded experiments against a task's HARNESS_PROBE stage and frozen ready specification, accept cancellation, and require administrator-reviewed exact-digest promotion or rollback. Confirmed post-merge learning creates only scoped, expiring candidate experience with evidence references. Successful task integration alone never activates an experience.

The durable experiment worker claims one queued experiment at a time with `FOR UPDATE SKIP LOCKED`, freezes the ready specification and project policy revision, and runs the pinned Harness² schedule through Harnessa ACP. Before dispatch, it scans the exact frozen Git tree's paths and fails closed if holdout, hidden, answer, grader, or rubric material is present; restricted evaluation data must remain in a separately permissioned evaluator channel. Baseline, candidate, and composed task executions each get their own `Run` ID, task-budget attribution, and probe-pool route. ACP probe calls use the experiment's configured probe alias; Harness²'s synchronous adapter callbacks marshal onto the worker's owning asyncio loop. Candidate and composed runs use the common approved repository base and candidate-specific guidance tree. The selected final harness is executed again and only that run counts as composed evidence. Redacted event-shape traces, patch digests, run outcomes, component diffs and known usage are persisted by content-addressed reference.

The queue worker refreshes a database heartbeat while an experiment is active. A worker whose heartbeat is older than three minutes has its active run rows fenced off and the experiment is requeued under a new claim attempt. Cancellation and changes to the frozen policy/specification stop further probe dispatch. If ACP execution and the brokered route are unavailable, the worker leaves the request queued and does not consume it; operators must configure the runner and route before it can start. If restricted evaluation paths exist in the frozen source tree, the experiment fails before any task model call or ACP sandbox launch.

## Validation and operational limits

The deterministic upstream demo uses the upstream `DemoAdapter` and `DemoProposer`, not a provider or hidden grader. It executes aggregate and sequential recursion with `k=1`. The product worker now has a task-execution path through the exact-revision ACP sandbox and probe budget broker. A live model-backed task experiment still requires an operator-configured probe alias, validated ACP runner, configured repository source, and a bounded authorized run. No such live execution was authorized or run in this milestone; no behavioral improvement or score is claimed. The worker runs against the configured repository snapshot and does not mount an external evaluator dataset; an independent holdout grader remains separate and is not part of this worker.

The upstream library is early-stage research software. Harnessa pins its dependency and isolates imports and schema assumptions behind one adapter. Revalidate the compatibility tests before changing the pin. Keep the host-policy and evaluator revisions immutable when updating upstream.

## Migration and recovery

The migration `52c0a42de71f` adds policy, experiment, probe, experience and promotion records. Review its SQL offline with `cd apps/api && uv run alembic upgrade head --sql`. In a configured deployment, use the standard migration command `cd apps/api && uv run alembic upgrade head`; the migration is additive. Failed post-merge learning remains independently retryable through the existing post-integration reconciler and does not repeat coding or merge. Promotion rollback is an append-only decision that restores a prior superseded exact version; it does not rewrite experiment evidence.

Worker operation requires the schema migration, a rootless Podman runner, the project's exact repository source mapping, ACP model broker enabled, and a compatible probe-pool route and budgets. On crash, inspect `harness_experiments.status`, `worker_heartbeat_at`, and the associated `runs.adapter_name` rows; stale runs are fenced and retried under a new `attempts` value. On cancellation, the worker stops the owning ACP containers and retains failure evidence. Do not use the upstream CLI's direct provider path. Complete a bounded authorized model-backed task and independent holdout grading before claiming live comparative improvement evidence.

## Upstream references

- [Harness² repository](https://github.com/google-research/harness2)
- [Pinned Apache-2.0 license](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/LICENSE)
- [Pinned setup documentation](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/docs/SETUP.md)
