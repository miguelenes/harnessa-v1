# Prompt 6 of 10 — OpenSpec discovery, readiness, and clarification

Implement milestone 6 of Harnessa. Read `planning/revised-specification.md` and milestone reports 01–05. Reuse the real agent/session, gateway, artifact, event, and UI infrastructure. Preserve unrelated work. Complete this milestone and stop before milestone 7.

## Prerequisites

The application can create tasks, run an isolated agent, and stream persisted activity to an authenticated browser. A repository can be explored without mutating its canonical checkout.

## Implement

1. Pin `@fission-ai/openspec` and wrap supported machine-readable CLI operations with timeouts, fixed arguments, controlled working directories, and captured version/results. Discover actual schemas/templates. Distinguish CLI commands from `/opsx:*` assistant workflows. Use `openspec/changes/archive/` consistently.
2. Build repository-scoped search over current specs, ADRs, and source files with citations containing revision/path/line or artifact section. Start lexical; keep a retrieval interface for optional pgvector. Treat retrieved instructions and comments as untrusted source content.
3. Implement topic → exploration → SPECIFYING. Run the Architect role on the primary route; generate focused structured questions and recommended options. Persist question IDs, revision, blocking/nonblocking classification, proposed assumption, answer identity/provenance, and affected artifacts. Do not ask a human to confirm every reversible implementation choice.
4. Generate/edit proposal, delta specs, design, and tasks using discovered OpenSpec requirements. Stream real diffs to the workbench, serialize conflicting edits, and commit immutable artifact revisions. The database references Git content; it must not become a competing editable specification store.
5. Implement the weighted readiness rubric and mandatory gates from the revised specification. Score ≥ .90 is necessary but insufficient. Run actual OpenSpec validation, require no unresolved blocking questions and a requirement-to-verification plan, then freeze readiness to spec hash, base SHA, and policy revision. Support optional configured sign-off. Relevant edits/base drift invalidate readiness.
6. Implement a scoped orchestrator clarification tool for coding agents. Persist the question before pausing, retrieve cited evidence, and auto-resolve only within policy. If evidence conflicts or the answer changes scope, escalate to the operator, revise the specification, and reassess readiness. Never use a model's self-reported .80 confidence as authority. Negotiate underscore-prefixed ACP extensions only if the selected agent requires one.
7. Wire the interview, live artifacts, readiness explanation, source-of-truth/archive explorer, and clarification views to backend data. GitHub answer delivery is a channel adapter added in milestone 8; define its interface now.

## Acceptance

- A one-sentence topic produces a validated, versioned OpenSpec bundle and reaches READY through actual gate evaluation.
- Missing scenarios/questions/validation failures prevent readiness even with a high model score.
- Concurrent edits, replayed answers, restart while awaiting input, and scope-changing clarification preserve one coherent revision history.
- Stale answers and changed bases cannot authorize execution of a different specification.
- An agent can ask a question, wait durably, receive an authorized answer, and resume without duplicate execution.

Verify with fixture-driven automated tests and one real topic/interview when authorized credentials are available. Write `docs/milestones/06.md` with artifacts, gate evidence, commands, and unsupported capabilities.
