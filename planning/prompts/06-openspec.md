# Prompt 6 of 13 — OpenSpec discovery, readiness, and clarification

Implement milestone 6 only after milestones 1–5 have completion evidence. Read the revised specification, `planning/architecture-decisions.md`, the planning index, and reports `docs/milestones/01.md` through `05.md`. Inspect the current code, versions, and worktree before editing; preserve unrelated changes. Complete this milestone and stop before milestone 7.

## Prerequisites

The application can create tasks, run an isolated agent, and expose persisted activity through authenticated APIs. A repository can be explored without mutating its canonical checkout.

## Scope at this stage

Deliver the topic/interview/readiness backend with lexical repository search, durable questions, and revisioned artifact APIs. This milestone precedes the web workbench: expose generated API contracts and API-driven acceptance now; milestone 8 adds the browser interview and artifact views. Milestone 7 extends the same search port with governed RAG/MCP. Reserve core evidence identifiers now under the shared contract; milestone 11 reconciles cross-system evidence without replacing these records.

## Implement

1. Pin `@fission-ai/openspec` and wrap supported machine-readable CLI operations with timeouts, fixed arguments, controlled working directories, and captured version/results. Discover actual schemas/templates. Distinguish CLI commands from `/opsx:*` assistant workflows. Use `openspec/changes/archive/` consistently.
2. Build repository-scoped search over current specs, ADRs, and source files with citations containing revision/path/line or artifact section. Start lexical; keep the same retrieval interface for milestone 7’s LlamaIndex/Qdrant pilot. Treat retrieved instructions and comments as untrusted source content.
3. Implement topic → exploration → SPECIFYING. Run the Architect role on the primary route; generate focused structured questions and recommended options. Persist question IDs, revision, blocking/nonblocking classification, proposed assumption, answer identity/provenance, and affected artifacts. Do not ask a human to confirm every reversible implementation choice.
4. Generate/edit proposal, delta specs, design, and tasks using discovered OpenSpec requirements. Expose real diffs through the artifact APIs for the milestone 8 workbench, serialize conflicting edits, and commit immutable artifact revisions. The database references Git content; it must not become a competing editable specification store.
5. Implement the weighted readiness rubric and mandatory gates from the revised specification. Score ≥ .90 is necessary but insufficient. Run actual OpenSpec validation, require no unresolved blocking questions and a requirement-to-verification plan, then freeze readiness to spec hash, base SHA, and policy revision. Support optional configured sign-off. Relevant edits/base drift invalidate readiness.
6. Implement a scoped orchestrator clarification tool for coding agents. Persist the question before pausing, retrieve cited evidence, and auto-resolve only within policy. If evidence conflicts or the answer changes scope, escalate to the operator, revise the specification, and reassess readiness. Never use a model's self-reported .80 confidence as authority. Negotiate underscore-prefixed ACP extensions only if the selected agent requires one.
7. Expose interview, live artifacts, readiness explanation, source-of-truth/archive explorer, and clarification contracts through authenticated APIs; browser wiring belongs to milestone 8. GitHub answer delivery is a channel adapter added in milestone 10; define its interface now.

## Acceptance

- A one-sentence topic produces a validated, versioned OpenSpec bundle and reaches READY through actual gate evaluation.
- Missing scenarios/questions/validation failures prevent readiness even with a high model score.
- Concurrent edits, replayed answers, restart while awaiting input, and scope-changing clarification preserve one coherent revision history.
- Stale answers and changed bases cannot authorize execution of a different specification.
- An agent can ask a question, wait durably, receive an authorized answer, and resume without duplicate execution.

Verify with fixture-driven automated tests and one real topic/interview when authorized credentials are available. Write `docs/milestones/06.md` with artifacts, gate evidence, commands, and unsupported capabilities.

## Delivery contract

Own only the subsystem named by this prompt and its existing adapters/UI projections. Preserve the shared authority, contract-generation, cost, recovery, and retention rules in `planning/architecture-decisions.md`. Produce the implementation, focused acceptance evidence, updated compatibility/setup notes, and `docs/milestones/06.md`. Record offline versus live results separately, exact versions/configuration and source revisions, pending external setup, recovery/rollback commands, and the next milestone’s prerequisites. Missing required evidence keeps the milestone active.

Apply the archive lifecycle in `planning/README.md` only after acceptance is evidenced; the future archive target is `planning/archive/completed/06-openspec.md`. Never run the next milestone as part of this request.
