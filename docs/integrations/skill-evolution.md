# Skill and harness evolution

This guide describes the M14 skill lifecycle implemented on top of Harnessa's existing Harness² experiments, budgets, artifact store, and promotion records. It does not create a second scheduler or grant tools through skill metadata.

## Compatibility boundary

Harnessa pins Harness² to [`1cfe254e8d25b15f37048c3730134686abe60335`](https://github.com/google-research/harness2/tree/1cfe254e8d25b15f37048c3730134686abe60335) and uses its OpenCode substrate. The adapter maps the pinned editable surface as follows:

| Harness² component | Accepted product path | Harnessa rule |
|---|---|---|
| System prompt | `systemprompt.md` | Editable guidance; host policy remains separate. |
| Guardrails | `guardrails.md` | Editable guidance; cannot replace authorization or sandbox policy. |
| Memory | `LongTermMEMORY.md` | Candidate content only; evidence and scope stay in PostgreSQL. |
| Tool guidance | `tool_descriptions/tool_guidance.md` | Text only; actual MCP/tool grants remain Harnessa-owned. |
| Subagents | `sub_agents/*/AGENT.md` | The current product validator disables subagent edits. |
| Skills | `skills/<lowercase-slug>/SKILL.md` | Strict YAML frontmatter and path validation; content is stored as immutable versions. |
| Scripts | `scripts/*.py` | Harness²'s reserved-script checks apply; Harnessa policy filenames are denied. |
| Plugins | `plugin/*.ts` | Harness²'s pinned OpenCode surface validator applies. |

The product validator rejects paths outside the pinned OpenCode surface and reserved immutable policy paths. The current lifecycle only derives skill candidates from changed, schema-valid `SKILL.md` files in a successful composed tree. `allowed-tools` and dependency metadata are declarations for review; they do not grant runtime capabilities. Hidden evaluation fixtures, grader code, access controls, and promotion policy are not editable components.

## Scope and provenance

`POST /projects/{project_id}/skills` accepts imported or operator-authored skill content and returns an untrusted candidate. The request binds the exact path, bytes, source kind, scope type, and scope identifier. Supported scopes are:

| Scope | `scope_id` | Use boundary |
|---|---|---|
| Task | Task UUID | Can be evaluated against that exact task only; it cannot be promoted across task boundaries. |
| Repository | Repository `target_id` in the project | Eligible only when every task in a grouped evaluation uses that repository. |
| Project | Project UUID | Available to runs in that project. |
| Organization | Organization UUID | Visible across projects in that organization; import and promotion require an organization administrator. |

Generated candidates are derived from composed skill files and retain source experiment/run IDs, trace evidence, confidence rationale, artifact digest, and compatibility snapshot. Imported, operator-authored, and generated sources remain distinct. Content and provenance fields are immutable in the ORM and, on PostgreSQL, protected by a database trigger. Status, stale reason, and revalidation time are lifecycle fields. The artifact store verifies the SHA-256 reference and size before evaluation or artifact display.

Repeated identical content in the same scope is idempotent. Different candidate content that overlaps an outstanding candidate's scope is marked conflicting and cannot enter evaluation or promotion until the conflicting candidate is resolved. Repository-specific candidates in different repositories do not conflict solely because they share a slug.

## Evaluation, promotion, and rollback

The Evaluation Center can pin one exact skill candidate to a bounded grouped evaluation. The version, path, content hash, CAS reference, license, scope, route/deployment/pricing records, task/specification revisions, base revisions, Harness² revision, executor/image, policy, and budget estimate are included in a content-digested manifest. `null` values explicitly mark unconfigured seeds, judge revisions, MCP-grant revisions, or evidence references; they are not evidence of an available hidden evaluator.

The selected skill is materialized into candidate/composed harness trees after its digest, schema, path, and symlink boundaries are checked. The baseline remains unmodified. Probe budgets are reserved through Harnessa's existing gateway ledger. Promotion is a separate administrator action; a successful candidate, self-report, or import is not sufficient. Skill promotion currently requires a passing holdout receipt bound to each child manifest, at least two evaluated tasks, successful matched/composed runs, and known probe costs. The decision records exact candidate identity and evidence; rollback reactivates the prior immutable version without deleting evaluations or source artifacts.

Active artifacts become stale when recorded compatibility inputs drift, the artifact is missing or invalid, the lifecycle expiry passes, or a conflict is detected. Stale artifacts are excluded from subsequent composed runs. A new version or a fresh evaluation is required; existing evidence is never rewritten.

## Independent holdout service

The optional worker adapter accepts a separately operated evaluator over HTTPS (loopback HTTP is allowed for local fixtures), sends the frozen manifest with a dedicated bearer credential, follows no redirects, bounds the response, and persists only a hash-bound receipt. Receipt feedback is not copied into agent-visible experiment results. The evaluator must own its private dataset and grader, authorize the Harnessa service identity, validate every frozen artifact and revision, and return a receipt bound to the submitted manifest digest and judge revision. It must not return expected answers or grading rubrics to the worker.

No independent endpoint, service identity, or private holdout dataset is configured in this checkout. Deterministic tests use an in-process fake receipt and prove boundary behavior only. They do not demonstrate independent grading, hidden-answer isolation against a live agent, or benchmark quality. With no evaluator configured, holdout status stays `unavailable` and skill promotion fails closed. Keep M14 active until the service and private dataset are provisioned and a bounded, authorized end-to-end task is accepted.

## Offline verification and operations

Run the API validation and deterministic skill tests from the repository root:

```sh
uv run ruff check apps/api tests
uv run mypy apps/api/harnessa/domain/skill_evolution.py apps/api/harnessa/adapters/harness2.py apps/api/harnessa/api/evaluation_routes.py apps/api/harnessa/api/harness_routes.py apps/api/harnessa/db/harness.py apps/api/harnessa/services/harness_adaptation.py apps/api/harnessa/services/harness_experiments.py apps/api/harnessa/services/post_integration.py apps/api/harnessa/services/verification_workflow.py
uv run pytest tests/test_skill_evolution.py tests/test_harness2_adapter.py tests/test_harness_evaluator.py -q
```

Use a fresh disposable PostgreSQL database ending in `_test` and a dedicated non-zero Redis database for migration and integration checks. The integration suite truncates the supplied test database and flushes that Redis database. Run `uv run alembic upgrade head` from `apps/api` when deploying the additive skill-provenance migration. The supported recovery operation is an administrator-reviewed rollback through `POST /projects/{project_id}/harness-experiences/{experience_id}/rollback`; database migration downgrade does not restore application lifecycle state.

No provider request or paid evaluation is needed for validation. Live evaluator, model-route, and agent-backed evidence must be reported separately and stay within the user's €20 aggregate ceiling.
