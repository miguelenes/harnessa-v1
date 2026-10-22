# Prompt 14 — Measured skill and harness evolution

Implement milestone 14 only after milestones 1–13 have completion evidence. Read the revised specification, `planning/architecture-decisions.md`, the planning index, and reports `docs/milestones/01.md` through `13.md`. Inspect the current code, versions, and worktree before editing; preserve unrelated changes. Complete this milestone and stop before milestone 15.

Extend prompt 12's harness candidate, evaluation, promotion, rollback, Harness Studio, and experience lifecycle. Add skill-specific provenance and scope where needed, but reuse those existing contracts and interfaces rather than creating a parallel learning or promotion subsystem.

## Prerequisites and owned scope

Milestone 13 release acceptance and all preceding milestone reports are complete. Extend the named subsystem through its existing ports, budgets and evidence records; use `planning/architecture-decisions.md` for shared failure/recovery and authority rules. Provider/agent access and spending remain explicitly configured, with offline fixtures for unavailable services.

## Goal

Turn verified task outcomes into scoped, reviewable skill and harness candidates that can be tested against immutable baselines, promoted under policy, observed in production, and rolled back. Keep task-level adaptation separate from persistent cross-task learning.

## Implement

1. Model a skill as an immutable, versioned artifact with source, author/producer, scope (task/repository/project/organization), license, dependencies, allowed tools, trust level, content hash, evidence links, expiry/revalidation date, and lifecycle status. Imported skills, generated candidates, and operator-authored skills must remain distinguishable.
2. Derive candidate lessons only from evidence-bearing outcomes. Require source run/evaluation links and confidence rationale; detect conflicts and duplicate lessons; expire or revalidate lessons when source policy, harness, model capability, or repository context changes. Never promote automatically from a single success or model self-report.
3. Define an explicit mapping between Harnessa's skill format and Harness²'s supported editable components for each pinned executor/substrate. Validate schemas and allowed paths. Candidate editors, probes, composers, and reflective components cannot edit immutable security policy, evaluator fixtures, permissions, benchmark data, or promotion rules.
4. Create reproducible evaluation manifests containing exact common base, candidate diff, task subset/seed, model/deployment and route policy, executor/substrate, harness revision, skill versions, tool/MCP grants, benchmark and judge revisions, budget, environment/image digest, and evidence artifact hashes. Separate public development feedback from hidden holdout evaluation.
5. Run bounded parallel/sequential Propose–Probe–Compose and the final composed harness in isolated, matched workspaces. Support cancellation/resume and cost/token/time ceilings. Prevent network access to hidden answers/rubrics; verify benchmark license and setup conditions before enabling runs. Unavailable prerequisites mean blocked, never a fabricated score.
6. Compare candidates to matched baselines and report per-task paired outcomes, sample size, uncertainty/variance where meaningful, cost, latency, failure/security regressions, and missing data. Promotion policy must specify required evidence, thresholds, human or configured approval, scope, and rollback target. Keep canary/rollout state and promotion decision auditable.
7. Support immediate rollback to a prior immutable harness/skill bundle, invalidate or flag evaluations affected by model/tool/data drift, and prevent rollback from changing already merged code or rewriting source evidence.
8. Add UI for candidate diffs, provenance, eval comparisons, policy decision, active scope, rollout health, expiry, and rollback. Do not show unsupported aggregate “learning score” or claim generalization from benchmark-only results.

## Acceptance

- A merged task with verified evidence can yield a reviewable candidate, but cannot change the active global harness without the configured evaluation and promotion gate.
- Re-running a frozen manifest either reproduces the exact configuration or clearly reports drift; all raw and derived artifacts remain content-addressed.
- Hidden graders and expected outputs remain outside agent, skill editor, retrieval, probe, and composer access; adversarial skill instructions cannot expand capabilities.
- Matched comparisons identify sample size and uncertainty; missing/failed tasks are represented rather than dropped or scored as zero.
- Promotion and rollback are revisioned, authorized, idempotent, and auditable. Rollback restores the selected prior bundle and does not erase evidence.
- Offline deterministic evaluation demonstrates lifecycle, isolation, manifests, and rollback without asserting real-world performance improvements.

Write `docs/integrations/skill-evolution.md`, update the compatibility matrix with tested Harness² revisions/executors, and write `docs/milestones/14.md` with exact evidence and limitations. Archive this prompt only after all criteria have been met.

## References to verify at implementation time

- [Harness² upstream component model and Propose–Probe–Compose](https://github.com/google-research/harness2) and its [setup requirements](https://github.com/google-research/harness2/blob/main/docs/SETUP.md).
- Verify upstream executor, component layout, benchmark support, setup/licensing, and result schema at the pinned commit. Keep Harnessa's policy and acceptance authority outside editable Harness² components.

## Delivery contract

Own only the subsystem named by this prompt and its existing adapters/UI projections. Preserve the shared authority, contract-generation, cost, recovery, and retention rules in `planning/architecture-decisions.md`. Produce the implementation, focused acceptance evidence, updated compatibility/setup notes, and `docs/milestones/14.md`. Record offline versus live results separately, exact versions/configuration and source revisions, pending external setup, recovery/rollback commands, and the next milestone’s prerequisites. Missing required evidence keeps the milestone active.

For review, assign an independent code-review agent to inspect the exact PR head. The reviewer posts progress and each change proposal directly in the PR conversation, with severity, affected path/line, evidence, impact, and a suggested change; a no-findings review posts a concise final comment explaining why no proposal is needed. The maintainer reads every proposal and either accepts it or responds with evidence and rationale. Limit each proposal's discussion to three PR comment/reply messages total, then record a final accepted or rejected decision and its rationale. Apply accepted changes on the PR branch and obtain a fresh review of the final head; resolve every review thread. A conversation comment is not a formal approval. Never self-approve or bypass repository protections.

During this run, put all milestone deliverables—including implementation, documentation, migrations, and `docs/milestones/14.md`—in one PR. Obtain an independent code review, resolve accepted findings, and merge the reviewed final head in this run after required checks and repository protections pass. Include the PR URL, reviewed head SHA, final review disposition, check results, and merge commit SHA in the final run summary. A blocked review or merge leaves milestone 14 incomplete and active; do not archive its prompt.

Apply the archive lifecycle in `planning/README.md` only after acceptance is evidenced; the future archive target is `planning/archive/completed/14-skill-evolution.md`. Never run the next milestone as part of this request.
