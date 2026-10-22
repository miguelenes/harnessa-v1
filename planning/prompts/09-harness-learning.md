# Prompt 9 of 10 — Harness² adaptation and learning

Implement milestone 9 of Harnessa. Read `planning/revised-specification.md` and milestone reports 01–08. Integrate pinned upstream Harness² through an adapter; do not replace it with a vaguely similar bespoke optimizer. Preserve unrelated work. Complete this milestone and stop before milestone 10.

## Prerequisites

Exact-revision sandbox execution, primary/probe routing, budget reservations, verification, immutable artifacts, and delivery outcomes work. Check upstream APIs/layout/licensing and executor compatibility before choosing the integration seam.

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

Write `docs/harness-integration.md` and `docs/milestones/09.md`. State precisely which behavior is upstream, which is product policy, and which real combinations remain unverified.
