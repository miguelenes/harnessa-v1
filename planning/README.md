# Harnessa: specification and 10 implementation prompts

This index tracks the Harnessa implementation prompts and completed milestone evidence. The application is implemented through milestone 2; later prompts remain active and are not claimed complete.

Start with [the revised specification](revised-specification.md), then execute the prompts below in order in the same repository. Each prompt names its prerequisites and includes implementation scope and acceptance criteria. They are separate implementation requests, not ten concurrent agent assignments.

| Order | Copy-ready prompt | Working outcome |
| --- | --- | --- |
| 1 | [Foundation, contracts, and access control](archive/completed/01-foundation.md) — **archived complete** | Runnable application skeleton, pinned dependencies, migrations, authentication, project authorization |
| 2 | [Durable orchestration and task lifecycle](archive/completed/02-orchestration.md) — **archived complete** | Crash-safe tasks, events, leases, bounded retries, cancellation |
| 3 | [LiteLLM routing and spend controls](prompts/03-model-gateway.md) | Primary/probe pools, explicit fallbacks, budget reservations, request ledger |
| 4 | [Isolated workspaces and ACP execution](prompts/04-acp-execution.md) | One real coding agent running safely, capability checks, resumable run records |
| 5 | [Web workspace and AG-UI streaming](prompts/05-web-ag-ui.md) | Live board, task inspector, reconnectable agent activity and interview UI |
| 6 | [OpenSpec discovery and clarification](prompts/06-openspec.md) | Topic → interview → validated frozen specification → READY |
| 7 | [Implementation, verification, and review](prompts/07-verification-review.md) | READY → isolated implementation → evidence-backed local review bundle |
| 8 | [GitHub synchronization and completion](prompts/08-github.md) | Issues, authorized answers, PRs, check reconciliation, merge and archive workflow |
| 9 | [Harness² adaptation and learning](prompts/09-harness-learning.md) | Bounded propose/probe/compose, isolated evaluations, governed harness promotion |
| 10 | [Complete control plane and release](prompts/10-release.md) | All six product views, operational tooling, acceptance evidence, self-hosted release |

## How to use

Give the implementation agent one prompt file at a time, with access to this directory and the application repository. Each prompt instructs the agent to read the revised specification and previous milestone evidence. Do not execute the attached source document's Google AI Studio instructions as an additional task.

For each milestone, require working code, focused verification, and `docs/milestones/NN.md` recording exact commands, outcomes, limitations, and the next milestone's prerequisites. Carry unresolved prerequisites forward explicitly. Never turn a blocked external integration into a fabricated success.

## Per-step archive lifecycle

A planning prompt is archived only after its implementation milestone meets its acceptance criteria and its `docs/milestones/NN.md` report records actual evidence, limitations, and follow-up prerequisites. At that point, move the prompt from `planning/prompts/` to `planning/archive/completed/` without changing its contents, update the matching index row here to link to the archived prompt and mark it **archived complete**, and leave later prompts active. Do not archive a milestone that is blocked, partially complete, or merely started; record that state in its milestone report and keep the prompt active. Keep `planning/revised-specification.md` and this index as the active planning references.

Milestones 1 and 2 have met this archive condition: their prompts are preserved in `planning/archive/completed/`, their index rows are marked **archived complete**, and their evidence is recorded in `docs/milestones/01.md` and `docs/milestones/02.md`.

The revised specification supersedes conflicting technical details in the source attachment. It preserves the product's seven pillars, ten board columns, six main views, and autonomous execution intent. The original attachment remains unchanged at `/home/menes/Downloads/acp_orchestrator_harness2_self_learning_platform_specification.md`.

AG-UI is recommended for agent-to-browser interaction. It reduces custom message/tool/state streaming work; it does not supply the scheduler, durable workflow engine, GitHub synchronization, sandbox, or learning engine. No complete third-party chat framework is required.

The web product must have a deliberate Harnessa brand and a documented UI system. Use the selected component framework as the source of reusable interaction patterns; do not assemble each screen from unrelated one-off controls. Generate suitable visual assets (such as a brand illustration or photographic artwork) with the image-generation capability when they improve the product, while keeping logos, icons, and other geometry crisp and maintainable as project-native SVG/vector assets unless a raster treatment is explicitly appropriate. See the brand and asset requirements in the revised specification and milestones 1, 5, and 10.

Research links and the rationale for technical corrections are in the revised specification. Dependencies must be checked and pinned during implementation; this plan does not assert that all proposed agent/provider combinations are supported today.
