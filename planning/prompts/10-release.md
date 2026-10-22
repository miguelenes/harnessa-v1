# Prompt 10 of 10 — Complete control plane and self-hosted release

Complete Harnessa end to end. Read `planning/revised-specification.md`, all prior milestone reports, and the current code. Preserve unrelated work. Audit actual behavior against every product requirement; a completed checklist without evidence is not sufficient.

## Prerequisites

Milestones 1–9 are integrated. Identify and repair missing integration work within this release scope. Surface external credential/provider/repository constraints explicitly rather than hiding them behind demo data.

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

Write `docs/acceptance-report.md`, operator/setup/recovery guides, the final compatibility matrix, and `docs/milestones/10.md`. Return what ships, exact verification results, remaining blockers, and user-run setup steps. Prepare a reviewable release; do not publish or deploy to an external environment without existing authorization.

## Archive this planning step

After every acceptance criterion above is met and `docs/milestones/10.md` records actual commands/results, limitations, and the next milestone's prerequisites, move this prompt from `planning/prompts/` to `planning/archive/completed/` without changing its contents. Update the matching milestone row in `planning/README.md` to link to the archived prompt and mark it **archived complete**. Do this only after the milestone is complete; if it is blocked or partial, keep this prompt active and record the state and evidence in the milestone report.
