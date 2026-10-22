# Prompt 8 of 10 — GitHub synchronization and completion

Implement milestone 8 of Harnessa. Read `planning/revised-specification.md` and milestone reports 01–07. Implement the GitHub channel and review publication adapters against existing domain services. Preserve unrelated work. Complete this milestone and stop before milestone 9.

## Prerequisites

Durable effect intents, scoped project grants, interviews, review bundles, revision-bound verification, and completion hooks exist. Use only explicitly authorized installation/repository targets for live checks.

## Implement

1. Add GitHub App installation/repository configuration, least-permission checks, installation-token handling, setup status, and explicit repository opt-in. Keep app private keys in secret references and out of agent sandboxes. Map authorized GitHub actors to product permissions.
2. Verify webhook HMAC over raw bytes, enforce size limits, store delivery identity/payload reference and processing intent transactionally, then acknowledge. Process asynchronously through the existing inbox/outbox system. Database uniqueness is authoritative; Redis caches do not determine whether an event is lost or duplicated.
3. Handle configured issue topics, issue edits, structured interview/clarification answers, PR lifecycle, review changes, and configured check/check-suite completion. Bind answers to question IDs/revisions and verify the author's authority. Unrelated prose, bots, forks, or arbitrary commenters cannot authorize decisions or execution.
4. Implement outbound issue/comments/status/branch/PR effects through durable intent records, stable correlation markers, GitHub object IDs, and reconciliation after ambiguous network failures. Suppress app echo loops without discarding legitimate human edits. Define field ownership and surface sync conflicts rather than silently overwriting GitHub or local changes.
5. Publish the real code/spec review bundle to a branch/PR within project policy. Reconcile checks only for the correct repository, PR, current head SHA, and required check set. A stale or unrelated failure must not restart implementation. Code changes after review invalidate prior acceptance as required by policy/branch protection.
6. Verify merge through GitHub state, record resulting SHAs, finalize archive provenance, run configured learning/cleanup hooks, and reach DONE only with durable completion evidence. Support default pre-merge archive and tracked post-merge maintenance PRs. Closed-unmerged/rejected PRs must have explicit recoverable terminal behavior.
7. Add periodic reconciliation for missed/reordered webhooks, rate limits, permission revocation, installation removal, and branch/head drift. Implement authenticated event/connection health UI and repair controls. Do not assume webhook delivery alone proves current truth.

## Acceptance

- Signature failures cannot create tasks. Duplicate/reordered/replayed deliveries do not duplicate cards, questions, comments, branches, or PRs.
- Unauthorized or stale comments cannot answer a question or approve a change.
- Crash after GitHub succeeds but before local acknowledgment is reconciled to the existing object.
- Correct-head check failure triggers bounded remediation; stale-head failure does not.
- A disposable authorized repository completes issue → interview → code/spec PR → verified merge → archive → cleanup → DONE.

Use replayable webhook fixtures and local API fakes in CI; record live evidence separately. If external setup is unavailable, ship exact setup/check instructions and mark live acceptance pending. Write `docs/milestones/08.md` including repository/installation scope without secrets.
