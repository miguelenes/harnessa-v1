# Prompt 7 of 10 — Implementation, verification, and review

Implement milestone 7 of Harnessa. Read `planning/revised-specification.md` and milestone reports 01–06. Connect existing specification, execution, clarification, budget, and UI services into a working local delivery path. Preserve unrelated work. Complete this milestone and stop before milestone 8.

## Prerequisites

The platform produces frozen READY specifications and can execute an isolated coding agent. GitHub publication is not required yet; implement review publication through a port used by milestone 8.

## Implement

1. Dispatch READY tasks only after validating current spec/base/policy revisions, lease, capabilities, and budget. Mount the approved specification and selected harness revision into an exact-base task workspace. Run implementation, record changed files and decisions, and update task checklist evidence without treating checkboxes as proof.
2. Add an independent verification runner using repository-configured, administrator-approved command manifests. Execute in a clean sandbox from the resulting code revision, with fixed limits and protected verification fixtures. Capture exit codes, outputs/artifacts, durations, and source revision. Agent changes to tests or verification configuration must be reviewable and must not silently weaken the acceptance gate.
3. Build a requirements/scenario verification matrix. Each entry is passed with evidence, failed, manual-review-required, or unverified. Include relevant regression, build/type/static checks and explicit scope limitations. A model statement cannot change failed evidence into a pass.
4. Implement bounded repair loops: failed visible development checks return useful sanitized feedback to implementation, within shared attempt/spend/time limits. Clarifications use the milestone 6 tool. Reserve a distinct hook for Harness² adaptation added in milestone 9; do not fake probe success.
5. Create an immutable local review bundle: code/spec diffs, requirement matrix, execution decisions, check evidence, cost, agent/model/harness/image identifiers, and exact head/base SHAs. Invalidate approval/checks after relevant edits. Implement reviewer actions and the publication provider port.
6. Prepare OpenSpec sync/archive in the review branch according to the revised specification's default policy. Preview the canonical spec diff, use a per-repository integration lock and expected base SHA, and verify the final review head. Keep completion pending until integration is confirmed. Define an alternate tracked post-merge maintenance-PR mode without pushing to the default branch.
7. Add a local/manual integration adapter for repositories without GitHub. Require explicit integration evidence instead of equating approval with merge. Record archive, learning, and cleanup as separately recoverable post-integration steps.

## Acceptance

- A task travels READY → IN_PROGRESS → VERIFYING → HUMAN_REVIEW with a real reviewable code/spec bundle.
- A deliberately broken implementation fails verification and either repairs within budget or stops visibly.
- Changed code/spec/head revisions invalidate stale verification and reviewer approval.
- No failed/manual/unverified required criterion is presented as automatically satisfied.
- Cancelling, crashing, or retrying verification/publication preparation does not duplicate changes or erase evidence.

Add a disposable local sample repository and run an end-to-end acceptance scenario. Write `docs/milestones/07.md` with the final bundle, commands/results, pending GitHub responsibilities, and any external execution limitations.
