# Harness² integration

## Compatibility pin

Harnessa pins Google Research Harness² at commit `1cfe254e8d25b15f37048c3730134686abe60335` and the upstream `Apache-2.0` license. The Python package is `harness2` version `0.1.0`, supports Python 3.12–3.13, and has no core Python runtime dependencies. The pin is recorded in `pyproject.toml` and `uv.lock`.

The adapter uses upstream `harness2.core.tree` validation/diff helpers, `harness2.core.schedule.run_task`, `harness2.core.context` leakage checks and the OpenCode substrate surface. Harnessa maps the eight upstream categories—system prompt, guardrails, memory, tool guidance, subagents, skills, scripts and plugins—to its product paths in `harnessa.adapters.harness2`. This is an adapter contract, not a claim that Harnessa's historical attachments are an upstream schema.

## Product policy boundary

Candidate content is limited to validated OpenCode guidance paths and bounded to 500 files / 2 MB. Installation policy, sandbox permissions, evaluator configuration, hidden answers, grading rubrics and promotion policy remain host-owned. The names `harnessa_policy.py`, `host_policy.json`, `evaluator_policy.json`, `promotion_policy.json` and `sandbox_policy.py` are rejected. Candidate edits are applied only under a resolved per-candidate `harness/` directory. Holdout-shaped files and grader verdicts are rejected before context is sent to the probe router. Model-backed probe requests use Harnessa's probe pool, task/project budget reservation and idempotency ledger; direct upstream provider CLIs are not used.

Project APIs expose manifest and probe metadata, persist bounded experiments against a task's HARNESS_PROBE stage and frozen ready specification, accept cancellation, and require administrator-reviewed exact-digest promotion or rollback. Confirmed post-merge learning creates only scoped, expiring candidate experience with evidence references. Successful task integration alone never activates an experience.

## Validation and operational limits

The deterministic upstream demo uses the upstream `DemoAdapter` and `DemoProposer`, not a provider or hidden grader. It executes aggregate and sequential recursion with `k=1`. The API records experiment requests durably. A live model-backed task experiment requires an operator-configured probe alias and an execution adapter that can safely run that project's task under each frozen harness candidate. No such live execution was authorized or run in this milestone; no behavioral improvement or score is claimed. The upstream demo does not substitute for the live task executor or independent holdout grading.

The upstream library is early-stage research software. Harnessa pins its dependency and isolates imports and schema assumptions behind one adapter. Revalidate the compatibility tests before changing the pin. Keep the host-policy and evaluator revisions immutable when updating upstream.

## Migration and recovery

The migration `52c0a42de71f` adds policy, experiment, probe, experience and promotion records. Review its SQL offline with `cd apps/api && uv run alembic upgrade head --sql`. In a configured deployment, use the standard migration command `cd apps/api && uv run alembic upgrade head`; the migration is additive. Failed post-merge learning remains independently retryable through the existing post-integration reconciler and does not repeat coding or merge. Promotion rollback is an append-only decision that restores a prior superseded exact version; it does not rewrite experiment evidence.

Next, connect the durable experiment queue to the existing isolated task execution adapter, persist matched baseline/candidate/composed execution evidence and budget usage, and run a bounded authorized model-backed task before claiming live comparison support.

## Upstream references

- [Harness² repository](https://github.com/google-research/harness2)
- [Pinned Apache-2.0 license](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/LICENSE)
- [Pinned setup documentation](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/docs/SETUP.md)
