# Harnessa: specification and implementation prompts

This index tracks the Harnessa implementation prompts and completed milestone evidence. Milestones 1–5 have completion reports and archived prompts; later work may be in progress but is not complete until its evidence is recorded.

Start with [the revised specification](revised-specification.md), then execute the prompts below in order in the same repository. Each prompt names its prerequisites and includes implementation scope and acceptance criteria. They are separate implementation requests, not concurrent agent assignments.

Milestones 1–3 retain their original scope and identifiers. Milestones 4–13 complete the initial release; 14–17 extend it. Historical prompts 1–3 retain their original “of 10” titles. Active release prompts use the revised boundary at 13.

Read [shared planning decisions](architecture-decisions.md) for runtime ownership, SDK choices, evidence, cost and recovery rules and [the integration assessment](integration-assessment.md) for protocol responsibilities. Every milestone requires actual completion evidence for all earlier milestones; report presence alone is insufficient. Verify milestone 3's report and acceptance before beginning milestone 4, including any required checks still pending in the working tree.

| Order | Copy-ready prompt | Working outcome |
| --- | --- | --- |
| 1 | [Foundation, contracts, and access control](archive/completed/01-foundation.md) — **archived complete** | Runnable application skeleton, pinned dependencies, migrations, authentication, project authorization |
| 2 | [Durable orchestration and task lifecycle](archive/completed/02-orchestration.md) — **archived complete** | Crash-safe tasks, events, leases, bounded retries, cancellation |
| 3 | [LiteLLM routing and spend controls](archive/completed/03-model-gateway.md) — **archived complete** | Interactive/primary and probe/batch pools, explicit fallbacks, budget reservations, request ledger |
| 4 | [Run orchestration decision](archive/completed/04-runtime-decision.md) — **archived complete (NO-GO)** | LangGraph parity spike; retain the current Harnessa scheduler |
| 5 | [Isolated workspaces and ACP execution](archive/completed/05-acp-execution.md) — **archived complete** | ACP SDK, trusted runner and safe execution |
| 6 | [OpenSpec discovery and clarification](archive/completed/06-openspec.md) — **archived complete** | Open PR audit/repair/merge/branch cleanup, merged design baseline, dependency/Python audit, 85% tests, AGENTS.md, hosted CI/review/merge, OpenSpec READY APIs |
| 7 | [Governed retrieval and MCP](archive/completed/07-retrieval-mcp.md) — **archived complete** | Reconcile every pending PR/branch from M6, establish clean `main`, add the root command helper and grounded LLM-ready `AGENTS.md`, then deliver LlamaIndex/Qdrant retrieval and scoped tools |
| 8 | [Web control plane and AG-UI](archive/completed/08-web-ag-ui.md) — **archived complete** | Real API-backed UI, typed events and replay |
| 9 | [Implementation, verification and review](archive/completed/09-verification-review.md) — **archived complete** | Revision-bound implementation and independent verification |
| 10 | [GitHub synchronization and completion](prompts/10-github.md) | Authorized issue/PR workflow and reconciled effects |
| 11 | [Cross-system evidence and provenance](prompts/11-evidence-plane.md) | Core delivery joins and future-source contracts |
| 12 | [Harness² adaptation and learning](prompts/12-harness-learning.md) | Matched baseline/candidate/composed evaluation |
| 13 | [Complete control plane and release](prompts/13-release.md) | All views, operations, restore and clean-install acceptance |
| 14 | [Measured skill and harness evolution](prompts/14-skill-evolution.md) | Scoped candidates, evaluation, promotion and rollback |
| 15 | [Governed A2A delegation](prompts/15-agent-delegation.md) | Bounded parent/child execution |
| 16 | [Structured decisions with Jev](prompts/16-jev-decision-workflows.md) | Evaluated routing and workflow decisions |
| 17 | [End-to-end LiteLLM optimization](prompts/17-litellm-end-to-end.md) | Model inventory, cache, compression, evals and operations |

## Parallel workstreams

These tracks are separate from the numbered dependency order. They must not be renamed or reported as numbered milestones.

| Track | Prompt | Outcome |
| --- | --- | --- |
| Web foundation | [Shared design system, marketing site, and static cloud portal](prompts/parallel-design-system-cloud-sites.md) | Shared React/Svelte components, Astro marketing site, static SvelteKit portal shell, and separate Cloudflare Pages deployments |

## How to use

Give the implementation agent one prompt at a time, with the revised specification, shared decisions and current evidence. Preserve existing domain and adapter contracts. Required external blockers remain explicit; optional live checks are recorded separately. Follow-on milestones 14–17 require the completed milestone 13 release.

For milestone 7, first reconcile every open PR and non-main branch, starting with milestone 6, then begin implementation from a clean `main` whose HEAD matches the refreshed `origin/main`; do not begin while any extra open PR or branch remains. For milestones 7–17, each run puts all deliverables—including implementation, documentation, migrations, and its milestone report—in one PR, obtains an independent code review, resolves accepted findings, and merges through required checks and repository protections during that same run. The review agent posts progress and proposals in the PR; the maintainer reviews each proposal, discusses it for no more than three PR comment/reply messages total, and records the final decision and rationale. Obtain a fresh review of the final head, resolve all review threads, and do not self-approve or bypass protections. Record the PR URL, reviewed head SHA, review disposition, check results, and merge SHA in the milestone report; include the merge commit SHA in the final run summary. If a required review, check, merge, or M7 inventory gate is blocked, document the blocker and keep the milestone active and unarchived.

## Renumbering map

Previous prompt → current prompt: 4→5, 5→8, 6→6, 7→9, 8→10, 9→12, 10→13, 11→11, 12→7, 13→14, 14→15, 15→16, 16→17. Prompt 4 is new. Use current numbers for execution and future reports. Completed reports and archived prompt contents remain historical records.

## Per-step archive lifecycle

A planning prompt is archived only after its implementation milestone meets its acceptance criteria and its `docs/milestones/NN.md` report records actual evidence, limitations, and follow-up prerequisites. For milestones 7–17, the milestone PR must also be reviewed and merged in that run before archiving. At that point, move the prompt from `planning/prompts/` to `planning/archive/completed/` without changing its contents, update the matching index row here to link to the archived prompt and mark it **archived complete**, and leave later prompts active. Do not archive a milestone that is blocked, partially complete, or merely started; record that state in its milestone report and keep the prompt active. Keep `planning/revised-specification.md` and this index as the active planning references.

Milestones 1–3 retain their currently recorded archive status and report paths. Milestones 10–17 remain active until their own acceptance evidence is recorded. Milestone 6's acceptance and protected merge evidence are recorded in `docs/milestones/06.md`; recheck current reports rather than inferring completion from file presence or from this index.


The revised specification supersedes conflicting technical details in the source attachment. It preserves the product's seven pillars, ten board columns, six main views, and autonomous execution intent. The original attachment remains unchanged at `/home/menes/Downloads/acp_orchestrator_harness2_self_learning_platform_specification.md`.

AG-UI is recommended for agent-to-browser interaction. It reduces custom message/tool/state streaming work; it does not supply the scheduler, durable workflow engine, GitHub synchronization, sandbox, or learning engine. No complete third-party chat framework is required.

The web product must have a deliberate Harnessa brand and a documented UI system. Use the selected component framework as the source of reusable interaction patterns; do not assemble each screen from unrelated one-off controls. Generate suitable visual assets (such as a brand illustration or photographic artwork) with the image-generation capability when they improve the product, while keeping logos, icons, and other geometry crisp and maintainable as project-native SVG/vector assets unless a raster treatment is explicitly appropriate. See the brand and asset requirements in the revised specification and milestones 1, 8, and 13.

Research links and the rationale for technical corrections are in the revised specification. Dependencies must be checked and pinned during implementation; this plan does not assert that all proposed agent/provider combinations are supported today.
