# Prompt 9 of 13 — Implementation, verification, and review

Implement milestone 9 only after milestones 1–8 have completion evidence. Read the revised specification, `planning/architecture-decisions.md`, the planning index, and reports `docs/milestones/01.md` through `08.md`. Inspect the current code, versions, and worktree before editing; preserve unrelated changes. Complete this milestone and stop before milestone 10.

## Prerequisites

The platform produces frozen READY specifications and can execute an isolated coding agent. GitHub publication is not required yet; implement review publication through a port used by milestone 10.

## Implement

1. Dispatch READY tasks only after validating current spec/base/policy revisions, lease, capabilities, and budget. Mount the approved specification and selected harness revision into an exact-base task workspace. Run implementation, record changed files and decisions, and update task checklist evidence without treating checkboxes as proof.
2. Add an independent verification runner using repository-configured, administrator-approved command manifests. Execute in a clean sandbox from the resulting code revision, with fixed limits and protected verification fixtures. Capture exit codes, outputs/artifacts, durations, and source revision. Agent changes to tests or verification configuration must be reviewable and must not silently weaken the acceptance gate.
3. Build a requirements/scenario verification matrix. Each entry is passed with evidence, failed, manual-review-required, or unverified. Include relevant regression, build/type/static checks and explicit scope limitations. A model statement cannot change failed evidence into a pass.
4. Implement bounded repair loops: failed visible development checks return useful sanitized feedback to implementation, within shared attempt/spend/time limits. Clarifications use the milestone 6 tool. Reserve a distinct hook for Harness² adaptation added in milestone 12; do not fake probe success.
5. Create an immutable local review bundle: code/spec diffs, requirement matrix, execution decisions, check evidence, cost, agent/model/harness/image identifiers, and exact head/base SHAs. Invalidate approval/checks after relevant edits. Implement reviewer actions and the publication provider port.
6. Prepare OpenSpec sync/archive in the review branch according to the revised specification's default policy. Preview the canonical spec diff, use a per-repository integration lock and expected base SHA, and verify the final review head. Keep completion pending until integration is confirmed. Define an alternate tracked post-merge maintenance-PR mode without pushing to the default branch.
7. Add a local/manual integration adapter for repositories without GitHub. Require explicit integration evidence instead of equating approval with merge. Record archive, learning, and cleanup as separately recoverable post-integration steps.

## Acceptance

- A task travels READY → IN_PROGRESS → VERIFYING → HUMAN_REVIEW with a real reviewable code/spec bundle.
- A deliberately broken implementation fails verification and either repairs within budget or stops visibly.
- Changed code/spec/head revisions invalidate stale verification and reviewer approval.
- No failed/manual/unverified required criterion is presented as automatically satisfied.
- Cancelling, crashing, or retrying verification/publication preparation does not duplicate changes or erase evidence.

Add a disposable local sample repository and run an end-to-end acceptance scenario. Write `docs/milestones/09.md` with the final bundle, commands/results, pending GitHub responsibilities, and any external execution limitations.

## Delivery contract

Own only the subsystem named by this prompt and its existing adapters/UI projections. Preserve the shared authority, contract-generation, cost, recovery, and retention rules in `planning/architecture-decisions.md`. Produce the implementation, focused acceptance evidence, updated compatibility/setup notes, and `docs/milestones/09.md`. Record offline versus live results separately, exact versions/configuration and source revisions, pending external setup, recovery/rollback commands, and the next milestone’s prerequisites. Missing required evidence keeps the milestone active.

For review, assign an independent code-review agent to inspect the exact PR head. The reviewer posts progress and each change proposal directly in the PR conversation, with severity, affected path/line, evidence, impact, and a suggested change; a no-findings review posts a concise final comment explaining why no proposal is needed. The maintainer reads every proposal and either accepts it or responds with evidence and rationale. Limit each proposal's discussion to three PR comment/reply messages total, then record a final accepted or rejected decision and its rationale. Apply accepted changes on the PR branch and obtain a fresh review of the final head; resolve every review thread. A conversation comment is not a formal approval. Never self-approve or bypass repository protections.

During this run, put all milestone deliverables—including implementation, documentation, migrations, and `docs/milestones/09.md`—in one PR. Obtain an independent code review, resolve accepted findings, and merge the reviewed final head in this run after required checks and repository protections pass. Include the PR URL, reviewed head SHA, final review disposition, check results, and merge commit SHA in the final run summary. A blocked review or merge leaves milestone 9 incomplete and active; do not archive its prompt.

Apply the archive lifecycle in `planning/README.md` only after acceptance is evidenced; the future archive target is `planning/archive/completed/09-verification-review.md`. Never run the next milestone as part of this request.
