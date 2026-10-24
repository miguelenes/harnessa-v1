# Prompt 13 of 13 — Complete control plane and self-hosted release

Implement milestone 13 only after milestones 1–12 have completion evidence. Read the revised specification, `planning/architecture-decisions.md`, the planning index, and reports `docs/milestones/01.md` through `12.md`. Inspect the current code, versions, and worktree before editing; preserve unrelated changes. Complete this milestone and stop before milestone 14.

## Prerequisites

Milestones 1–12 are integrated with their completion evidence. Identify and repair missing integration work within this release scope. Surface external credential/provider/repository constraints explicitly rather than hiding them behind demo data.

## Implement

1. Complete all six production views: board with five-tab inspector, OpenSpec source/change/archive explorer, Harness Studio, evals, model routing, and GitHub/event inspector. Include search/filter/pagination, revision links, meaningful errors, keyboard access, responsive layouts, and supported mutation controls. Remove production simulation buttons; retain fault injection only in explicit development/test tooling.
2. Finish setup/onboarding for administrator identity, repository grants, agent/adapter selection, route capability checks, primary/probe budgets, GitHub installation, and runtime health. Offer configuration verification before any paid task or benchmark starts. Show disabled/missing integrations clearly.
3. Build the eval center with local repository suites plus adapters for WorkBuddy-Bench, JobBench, and LAB. Respect dataset licenses and setup/runtime requirements. Support dry-run estimates, bounded subsets, cancellation/resume, matched baselines, immutable manifests, cost/latency/failure summaries, sample size, and uncertainty where meaningful. Missing prerequisites are blocked/unavailable, never a zero or fabricated score.
4. Add structured redacted logs, correlation IDs, metrics and traces across task/run/model/GitHub/worker operations. Expose queue age, stale leases, dead letters, routing availability, unreconciled spend/effects, artifact growth, and cleanup failures. Provide operator recovery commands with authorization and audit.
5. Ship pinned Compose deployment, a minimal trusted runner installation, reverse-proxy/TLS guidance, secure bootstrap, secret-reference examples, migrations, readiness checks, retention, disk-pressure handling, graceful shutdown, and a doctor command. Document supported host/runtime versions and the security boundary of containers.
6. Implement and exercise backup/restore for database, specification/harness Git data, artifacts, and configuration references; handle secret restoration separately. Ship upgrade/rollback instructions, an application rollback compatibility policy, and owned-resource uninstall/cleanup procedures.
7. Add CI for focused unit/contract/database/Redis/adapter tests and browser acceptance. Verify authorization boundaries, injection-resistant answer handling, redaction, sandbox policy, stream reconnect, crash recovery, budget contention, stale-head GitHub events, and evaluator isolation. Follow Browser Harness instructions for browser interaction; use repository-approved browser tooling for committed regression tests.
8. Audit every shipped screen against `docs/design-system.md`: shared components and tokens are used consistently, the logo/wordmark/favicon/app icon work in their required contexts, all selected generated raster assets are present with provenance and production integration, and no placeholder or broken assets remain. Check responsive layouts, accessible contrast and names, alt text, light/dark behavior, and reduced motion. Update the brand/design-system and asset inventory documentation to match the actual release.

## Final acceptance

- From a clean self-hosted installation, an operator can configure an explicitly authorized disposable repository and route, create a one-line topic, answer questions, reach validated READY, run an isolated real agent, resolve a clarification, verify the exact code/spec head, review and merge a PR, finalize archive/cleanup, inspect a learning candidate, and roll back a harness promotion.
- Exercise a bounded probe run, 429 failover, worker restart, webhook replay, and browser reconnect. Confirm no duplicate external action or budget escape.
- Demonstrate deterministic offline acceptance separately from live provider/GitHub evidence.
- Restore a backup into a fresh instance and prove task/artifact/provenance continuity.
- Every required scenario has evidence or is explicitly listed as an external release blocker. No unsupported integration is advertised as verified.

Write `docs/acceptance-report.md`, operator/setup/recovery guides, the final compatibility matrix, and `docs/milestones/13.md`. Return what ships, exact verification results, remaining blockers, and user-run setup steps. Prepare a reviewable release and merge the milestone PR as required below; do not publish packages or deploy to an external environment without existing authorization.

## Delivery contract

Own only the subsystem named by this prompt and its existing adapters/UI projections. Preserve the shared authority, contract-generation, cost, recovery, and retention rules in `planning/architecture-decisions.md`. Produce the implementation, focused acceptance evidence, updated compatibility/setup notes, and `docs/milestones/13.md`. Record offline versus live results separately, exact versions/configuration and source revisions, pending external setup, recovery/rollback commands, and the next milestone’s prerequisites. Missing required evidence keeps the milestone active.

For review, assign an independent code-review agent to inspect the exact PR head. The reviewer posts progress and each change proposal directly in the PR conversation, with severity, affected path/line, evidence, impact, and a suggested change; a no-findings review posts a concise final comment explaining why no proposal is needed. The maintainer reads every proposal and either accepts it or responds with evidence and rationale. Limit each proposal's discussion to three PR comment/reply messages total, then record a final accepted or rejected decision and its rationale. Apply accepted changes on the PR branch and obtain a fresh review of the final head; resolve every review thread. A conversation comment is not a formal approval. Never self-approve or bypass repository protections.

During this run, put all milestone deliverables—including implementation, documentation, migrations, and `docs/milestones/13.md`—in one PR. Obtain an independent code review, resolve accepted findings, and merge the reviewed final head in this run after required checks and repository protections pass. Include the PR URL, reviewed head SHA, final review disposition, check results, and merge commit SHA in the final run summary. A blocked review or merge leaves milestone 13 incomplete and active; do not archive its prompt.

Apply the archive lifecycle in `planning/README.md` only after acceptance is evidenced; the future archive target is `planning/archive/completed/13-release.md`. Never run the next milestone as part of this request.
