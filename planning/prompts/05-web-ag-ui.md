# Prompt 5 of 10 — Web workspace and AG-UI streaming

Implement milestone 5 of Harnessa. Read `planning/revised-specification.md` and milestone reports 01–04. Use existing APIs, domain events, and run records. Preserve unrelated work. Complete this milestone and stop before milestone 6.

## Prerequisites

Authenticated project/task APIs, durable event replay, and an execution adapter exist. Recheck pinned AG-UI SDK APIs in official documentation before implementation.

## Implement

1. Build a responsive application shell with project/repository selection and routes for board, OpenSpec explorer, Harness Studio, evals, routing, and events. Follow `docs/design-system.md` and the shared Tailwind/shadcn component layer from milestone 1. Apply the defined Harnessa logo, typography, semantic color roles, icon style, and light/dark behavior consistently. Features not implemented yet show honest setup/availability states; do not fill them with invented activity or scores.
2. Build the ten-column board with dnd-kit, server-authorized transitions, optimistic version checks, rejected-drag explanations, keyboard alternatives, and reconciliation after conflicts. Show task stage plus run/blocked status, specification revision/readiness, active agent/route, and recorded spend.
3. Build the task inspector with interview, artifact/delta, run activity/decisions, clarification, and probe/review tabs. Implement usable loading, empty, error, permission-denied, cancellation, and reconnect states. Only expose actions supported by current backend policy.
4. Add an AG-UI adapter projecting persisted run events to standard run, step, text, tool, state snapshot/delta, and namespaced custom events. Use official Python encoding/types and TypeScript SDK consumption. Keep browser state distinct from domain authority. Use custom typed events for product-specific questions/readiness where appropriate rather than inventing standard AG-UI types.
5. Implement durable stream cursors, ordered replay, duplicate suppression, heartbeats, backpressure, bounded buffers, and snapshot recovery when the cursor has expired. Document the app-specific resume envelope and compatibility with the selected client. Do not assume the SDK provides durable replay. Project-wide board updates can use the ordinary domain SSE endpoint; AG-UI handles agent interaction.
6. Define authenticated, idempotent HTTP commands for submitting answers, cancelling runs, and editing supported artifacts. Closing a browser stream must not cancel a worker run; an explicit cancel command does. Enforce authorization before subscription and after project grants change.
7. Use safe Markdown rendering and lazy-loaded Monaco diffs. Prefer structured redacted tool/run activity over an interactive terminal. Add xterm only if a real authorized terminal transport requires it; never expose raw protocol/secret output by default.
8. Extend shared components and tokens when a new interaction pattern is needed; do not copy/paste styles or add a competing component framework. Use project-native vector icons for controls and states. Integrate selected brand imagery only where it improves hierarchy or comprehension, with correct alt text, responsive sizing, optimized delivery, and reduced-motion handling.

## Acceptance

- Two authorized browser sessions see real backend task transitions consistently; stale commands resolve visibly.
- Disconnect/reconnect during a run preserves message/tool order and does not duplicate questions or start a second run.
- Snapshot/delta handling recovers from missing history and malformed/unknown events safely.
- Unauthorized subscriptions and mutations fail; browser rendering cannot execute untrusted Markdown/script content.
- Keyboard board actions, focus management, and principal responsive states work.

Follow the repository's Browser Harness instructions for actual browser verification. Write `docs/milestones/05.md` with protocol mapping, reconnect semantics, UI evidence, shared component coverage, brand consistency, and remaining feature dependencies.
