# Prompt 12 of 13 — Harness² adaptation and learning

Implement milestone 12 only after milestones 1–11 have completion evidence. Read the revised specification, `planning/architecture-decisions.md`, the planning index, and reports `docs/milestones/01.md` through `11.md`. Inspect the current code, versions, and worktree before editing; preserve unrelated changes. Complete this milestone and stop before milestone 13.

## Prerequisites

Exact-revision sandbox execution, interactive/primary and probe/batch routing, budget reservations, verification, immutable artifacts, and delivery outcomes work. Check upstream APIs/layout/licensing and executor compatibility before choosing the integration seam.

## Evaluation and release handoff

Provide the minimal reusable evaluation runner/manifests required for matched Harness² comparisons here. Milestone 13 completes the eval center and benchmark adapters; do not depend on that future UI or create another evaluator. Consume milestone 11 evidence contracts and extend the existing Harness Studio with real baseline/candidate/composed runs before release acceptance.

## Implement

1. Pin upstream Harness² and record its revision/license. Map its eight component categories to a validated product manifest. Distinguish configurable guidance from immutable installation policy. Do not assume the attachment's eight filenames are the upstream schema. Isolate adapter-specific differences and publish a compatibility test.
2. Implement task-level Propose → Probe → Compose using a common base harness, identical task/base/spec inputs, isolated candidate workspaces, bounded k=1..3, and parallel/sequential modes. Route probes to the configured probe pool with shared task budget/concurrency limits. Candidate runs cannot mutate canonical specs, another candidate, or the base workspace.
3. Capture matched baseline/candidate behavioral traces, edited component diffs, run outcomes, token/cost/time, model routes, and image/tool revisions. Execute the composed harness again; do not report composition itself as a measured improvement. Keep adaptation lineage separate from the final implementation branch.
4. Enforce independent evaluation isolation: editors/probes/composer/reflector cannot access held-out answers, graders, hidden tests, numeric scores, or grading rubrics. Normal visible development checks remain a separate channel. Run holdout grading after the adaptation artifacts are finalized and prevent leakage through traces, retrieval, storage permissions, or UI-provided agent context.
5. Implement cross-task improvement-harness learning as candidate experience records with evidence references, repository/domain scope, conflicts, expiry/revalidation, and version history. A successful PR alone is insufficient evidence for a global rule. Start each task from its selected immutable execution baseline; reuse improvement-harness experience through the appropriate upstream mechanism.
6. Add candidate evaluation and explicit policy-driven promotion/rollback. Default to reviewed promotion of shared harness versions; any automatic policy must have fixed regression/security/cost gates. Candidate guardrails, scripts, plugins, and skills cannot relax host permissions or modify evaluator/promotion policy.
7. Wire HARNESS_PROBE and LEARNING branch behavior to durable orchestration. Learning failure after merge remains independently retryable and never repeats coding/merge. Add Harness Studio tree/history/diffs, probe comparison, experience evidence, and promotion actions.

## Acceptance

- A deterministic upstream-compatible demo exercises both recursion modes without provider credentials.
- An authorized small model-backed run records baseline, candidates, composed execution, costs, and provenance when available; do not invent gains otherwise.
- Tests prove budget bounds, workspace isolation, prohibited-policy edits, held-out feedback separation, safe cancellation, and promotion rollback.
- No automatically active global harness is created just because a task merged.

Write `docs/harness-integration.md` and `docs/milestones/12.md`. State precisely which behavior is upstream, which is product policy, and which real combinations remain unverified.

## Delivery contract

Own only the subsystem named by this prompt and its existing adapters/UI projections. Preserve the shared authority, contract-generation, cost, recovery, and retention rules in `planning/architecture-decisions.md`. Produce the implementation, focused acceptance evidence, updated compatibility/setup notes, and `docs/milestones/12.md`. Record offline versus live results separately, exact versions/configuration and source revisions, pending external setup, recovery/rollback commands, and the next milestone’s prerequisites. Missing required evidence keeps the milestone active.

For review, assign an independent code-review agent to inspect the exact PR head. The reviewer posts progress and each change proposal directly in the PR conversation, with severity, affected path/line, evidence, impact, and a suggested change; a no-findings review posts a concise final comment explaining why no proposal is needed. The maintainer reads every proposal and either accepts it or responds with evidence and rationale. Limit each proposal's discussion to three PR comment/reply messages total, then record a final accepted or rejected decision and its rationale. Apply accepted changes on the PR branch and obtain a fresh review of the final head; resolve every review thread. A conversation comment is not a formal approval. Never self-approve or bypass repository protections.

During this run, put all milestone deliverables—including implementation, documentation, migrations, and `docs/milestones/12.md`—in one PR. Obtain an independent code review, resolve accepted findings, and merge the reviewed final head in this run after required checks and repository protections pass. Include the PR URL, reviewed head SHA, final review disposition, check results, and merge commit SHA in the final run summary. A blocked review or merge leaves milestone 12 incomplete and active; do not archive its prompt.

Apply the archive lifecycle in `planning/README.md` only after acceptance is evidenced; the future archive target is `planning/archive/completed/12-harness-learning.md`. Never run the next milestone as part of this request.
