# Harnessa: specification and implementation prompts

This index tracks the Harnessa implementation prompts and completed milestone evidence. Milestones 1 and 2 are the only milestones with completion reports and archived prompts; later work may be in progress but is not complete until its evidence is recorded.

Start with [the revised specification](revised-specification.md), then execute the prompts below in order in the same repository. Each prompt names its prerequisites and includes implementation scope and acceptance criteria. They are separate implementation requests, not concurrent agent assignments.

Prompts 1–10 are the initial release sequence, which is why their headings say “of 10.” Prompts 11–16 are gated follow-on milestones and must not begin until milestone 10 is integrated and reported complete.

See [the integration assessment](integration-assessment.md) for shared boundaries and sequencing rationale for prompts 11–16.

| Order | Copy-ready prompt | Working outcome |
| --- | --- | --- |
| 1 | [Foundation, contracts, and access control](archive/completed/01-foundation.md) — **archived complete** | Runnable application skeleton, pinned dependencies, migrations, authentication, project authorization |
| 2 | [Durable orchestration and task lifecycle](archive/completed/02-orchestration.md) — **archived complete** | Crash-safe tasks, events, leases, bounded retries, cancellation |
| 3 | [LiteLLM routing and spend controls](prompts/03-model-gateway.md) | Interactive/primary and probe/batch pools, explicit fallbacks, budget reservations, request ledger |
| 4 | [Isolated workspaces and ACP execution](prompts/04-acp-execution.md) | One real coding agent running safely, capability checks, resumable run records |
| 5 | [Web workspace and AG-UI streaming](prompts/05-web-ag-ui.md) | Live board, task inspector, reconnectable agent activity and interview UI |
| 6 | [OpenSpec discovery and clarification](prompts/06-openspec.md) | Topic → interview → validated frozen specification → READY |
| 7 | [Implementation, verification, and review](prompts/07-verification-review.md) | READY → isolated implementation → evidence-backed local review bundle |
| 8 | [GitHub synchronization and completion](prompts/08-github.md) | Issues, authorized answers, PRs, check reconciliation, merge and archive workflow |
| 9 | [Harness² adaptation and learning](prompts/09-harness-learning.md) | Bounded propose/probe/compose, isolated evaluations, governed harness promotion |
| 10 | [Complete control plane and release](prompts/10-release.md) | All six product views, operational tooling, acceptance evidence, self-hosted release |
| 11 | [Cross-system evidence and result provenance](prompts/11-evidence-plane.md) | Correlated, immutable evidence spanning LiteLLM, Harness², GitHub, AG-UI, MCP, and skills |
| 12 | [Governed retrieval and MCP context](prompts/12-retrieval-mcp.md) | Permission-aware RAG and scoped tools grounded in versioned repository and run evidence |
| 13 | [Measured skill and harness evolution](prompts/13-skill-evolution.md) | Evidence-derived candidates, isolated evaluations, controlled promotion and rollback |
| 14 | [Governed agent-to-agent delegation](prompts/14-agent-delegation.md) | Durable, budgeted delegation across ACP agents and optional remote A2A agents |
| 15 | [Structured decision workflows with Jev](prompts/15-jev-decision-workflows.md) | Typed, evaluated decisions for routing, workflow branching, review, and cost control |
| 16 | [End-to-end LiteLLM integration and optimization](prompts/16-litellm-end-to-end.md) | Version-pinned gateway operations, refreshed zero-price model inventory, Qdrant, caching, compression, evaluations, routing, A2A, and measured cost/quality controls |

## How to use

Give the implementation agent one prompt file at a time, with access to this directory and the application repository. Each prompt instructs the agent to read the revised specification and previous milestone evidence. Prompts 11–16 extend the initial product release with evidence, interoperability, improvement, structured decisions, and complete LiteLLM integration; they do not replace or silently widen prompt 10. Do not execute the attached source document's Google AI Studio instructions as an additional task.

For each milestone, require working code, focused verification, and `docs/milestones/NN.md` recording exact commands, outcomes, limitations, and the next milestone's prerequisites. Carry unresolved prerequisites forward explicitly. Never turn a blocked external integration into a fabricated success.

## Per-step archive lifecycle

A planning prompt is archived only after its implementation milestone meets its acceptance criteria and its `docs/milestones/NN.md` report records actual evidence, limitations, and follow-up prerequisites. At that point, move the prompt from `planning/prompts/` to `planning/archive/completed/` without changing its contents, update the matching index row here to link to the archived prompt and mark it **archived complete**, and leave later prompts active. Do not archive a milestone that is blocked, partially complete, or merely started; record that state in its milestone report and keep the prompt active. Keep `planning/revised-specification.md` and this index as the active planning references.

Milestones 1 and 2 have met this archive condition: their prompts are preserved in `planning/archive/completed/`, their index rows are marked **archived complete**, and their evidence is recorded in `docs/milestones/01.md` and `docs/milestones/02.md`. Milestones 3–10 remain active until their own acceptance evidence is recorded. Prompts 11–16 depend on the completed milestone 10 release and then follow their listed order. Prompt 16 reconciles and completes LiteLLM work from prompt 3 and connected contracts from prompts 11–15; it must extend those systems rather than create parallel model, retrieval, evaluation, decision, or delegation services.

The revised specification supersedes conflicting technical details in the source attachment. It preserves the product's seven pillars, ten board columns, six main views, and autonomous execution intent. The original attachment remains unchanged at `/home/menes/Downloads/acp_orchestrator_harness2_self_learning_platform_specification.md`.

AG-UI is recommended for agent-to-browser interaction. It reduces custom message/tool/state streaming work; it does not supply the scheduler, durable workflow engine, GitHub synchronization, sandbox, or learning engine. No complete third-party chat framework is required.

The web product must have a deliberate Harnessa brand and a documented UI system. Use the selected component framework as the source of reusable interaction patterns; do not assemble each screen from unrelated one-off controls. Generate suitable visual assets (such as a brand illustration or photographic artwork) with the image-generation capability when they improve the product, while keeping logos, icons, and other geometry crisp and maintainable as project-native SVG/vector assets unless a raster treatment is explicitly appropriate. See the brand and asset requirements in the revised specification and milestones 1, 5, and 10.

Research links and the rationale for technical corrections are in the revised specification. Dependencies must be checked and pinned during implementation; this plan does not assert that all proposed agent/provider combinations are supported today.
