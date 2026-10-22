# Prompt 4 of 10 — Isolated workspaces and ACP execution

Implement milestone 4 of Harnessa. Read `planning/revised-specification.md` and milestone reports 01–03. Use the existing scheduler and model gateway. Preserve unrelated work. Complete this milestone and stop before milestone 5.

## Prerequisites

Task leases, cancellation, model budgets, and route configuration are functional. Inspect available container tooling and agent versions; do not assume installed names establish protocol compatibility.

## Implement

1. Build a narrow trusted runner/broker and a rootless Podman sandbox backend. Keep container-engine access outside agent containers. Provide a backend interface for Docker without claiming untested parity.
2. Provision per-run managed clones/workspaces at exact base commits. If using worktrees, parent them under dedicated managed clones so host/shared Git metadata is not exposed. Record ownership, base SHA, branch, image digest, mounts, egress grants, resource limits, deadlines, and cleanup policy. Preserve the user's original checkout and dirty state.
3. Add an agent registry with native ACP versus adapter/bridge transport, pinned binary/package version, protocol version, resume/permission/file/terminal capabilities, model-route compatibility, and setup status. Ship one genuine supported ACP path first. Document remaining requested agents—OpenCode, Codex, Claude Code, Goose, Gemini CLI—with verified, adapter-required, or unavailable status.
4. Implement ACP initialization/capability negotiation, session creation, prompt lifecycle, updates, permission requests, cancellation, process exit/error handling, bounded JSON-RPC parsing, and request correlation. Resume only where supported; otherwise create an explicit new attempt from durable context. Keep protocol stdout separate from stderr logs.
5. Route filesystem/terminal operations through scoped policy enforcement. Deny path traversal, unauthorized symlink access, host secrets/socket access, and unauthorized network access. Bind runner output publication to a current fencing token.
6. Stream redacted run activity to the durable event/artifact services. Capture concise decision records, changed-file manifests, exit status, and checkpoints. Do not request raw private reasoning. Implement owned-resource cleanup and startup orphan reconciliation without removing other users' resources.
7. Reserve the clarification tool interface for milestone 6. Do not implement the attachment's `session/ask_orchestrator` as a standard ACP method. Prefer an agent-supported scoped tool bridge; custom ACP extensions must follow the protocol's extension rules.

## Acceptance

- A protocol fixture covers malformed frames, timeouts, cancellation, process crash, permissions, and unsupported resume.
- A real supported agent completes a small disposable repository edit through the sandbox and configured route when credentials/spending are authorized; otherwise report the precise external blocker.
- Isolation checks prove no host checkout, other task workspace, secret, or engine socket is accessible.
- Lease expiry prevents stale artifact/state publication; cancellation terminates owned processes; cleanup preserves artifacts required for audit.

Write sandbox policy, agent compatibility/setup instructions, and `docs/milestones/04.md`. Distinguish protocol fixture success, real agent execution, and provider-backed evidence.
