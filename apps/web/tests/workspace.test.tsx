import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Workspace } from "../components/workspace";

const { push, router } = vi.hoisted(() => {
  const push = vi.fn();
  return { push, router: { push } };
});
vi.mock("next/navigation", () => ({ useRouter: () => router }));
vi.mock("next/link", () => ({ default: ({ href, children, ...props }: React.AnchorHTMLAttributes<HTMLAnchorElement>) => <a href={href} {...props}>{children}</a> }));
vi.mock("next/dynamic", () => ({ default: () => () => null }));
vi.mock("@dnd-kit/core", () => ({
  DndContext: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  KeyboardSensor: function KeyboardSensor() {},
  PointerSensor: function PointerSensor() {},
  useDraggable: () => ({ attributes: { "aria-describedby": "DndDescribedBy-0" }, listeners: {}, setNodeRef: () => {}, isDragging: false }),
  useDroppable: () => ({ setNodeRef: () => {}, isOver: false }),
  useSensor: () => ({}),
  useSensors: (...sensors: unknown[]) => sensors,
}));

const project = { id: "project-1", name: "Alpha", slug: "alpha" };
const task = {
  id: "task-1",
  project_id: project.id,
  title: "Prepare release",
  description: "",
  priority: 1,
  stage: "BACKLOG",
  run_status: "QUEUED",
  blocked_reason: null,
  return_stage: null,
  version: 3,
  created_at: "2026-10-23T00:00:00Z",
  updated_at: "2026-10-23T00:00:00Z",
  specification_readiness_state: "READY",
  specification_revision: 2,
  allowed_transitions: ["SPECIFYING"],
};
const interview = { id: "interview-1", revision: 1, readiness_state: "DRAFT" };
const question = { id: "question-1", question: "Which users can approve?", status: "open", classification: "functional", affected_artifacts: ["spec.md"] };
const artifacts = {
  proposal_md: "# Proposal\n\n<script>alert(1)</script>\n\nSafe text",
  spec_md: "# Spec",
  design_md: "# Design",
  tasks_md: "# Tasks",
  diff: "--- a/spec.md\n+++ b/spec.md",
};

function response(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

describe("workspace routes and task board", () => {
  beforeEach(() => {
    push.mockReset();
    localStorage.clear();
    sessionStorage.clear();
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/projects")) return response([project]);
      if (url.includes("/events/stream")) return response({}, 403);
      if (url.endsWith("/repositories")) return response([]);
      if (url.endsWith("/models/usage")) return response({ billed_usd: "0.00", estimated_usd: "0.00", input_tokens: 0, output_tokens: 0, unknown_usage_requests: 0, pending_reservations: 0 });
      if (url.endsWith("/models/requests?limit=50")) return response([]);
      if (url.endsWith("/models/budget")) return response({}, 404);
      if (url.endsWith("/tasks?limit=100")) return response([task]);
      if (url.endsWith("/tasks/task-1/runs")) return response([]);
      if (url.endsWith("/tasks/task-1/capabilities")) return response({ allowed_commands: [], allowed_transitions: ["SPECIFYING"] });
      if (url.endsWith("/tasks/task-1/specification-interview")) return response({}, 404);
      if (url.endsWith("/tasks/task-1/transition") && init?.method === "POST") {
        return response({ ...task, stage: "SPECIFYING", version: 4 });
      }
      if (url.endsWith("/tasks/task-1")) return response({ ...task, stage: "SPECIFYING", version: 4 });
      return response({}, 404);
    }));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders the ten-column board, task/run/spec status, and keyboard transition controls", async () => {
    render(<Workspace section="board" />);
    expect(await screen.findByRole("button", { name: /Prepare release/ })).toBeTruthy();
    for (const label of ["Backlog", "Specifying", "Ready", "In progress", "Verifying", "Human review", "Learning", "Done", "Clarifying", "Harness probe"]) {
      expect(screen.getByRole("region", { name: `${label} column` })).toBeTruthy();
    }
    expect(screen.getByText("QUEUED")).toBeTruthy();
    expect(screen.getByText("Spec READY · r2")).toBeTruthy();
    const dragHandle = screen.getByRole("button", { name: "Drag task card task-1" });
    expect(dragHandle.getAttribute("aria-describedby")).toContain("DndDescribedBy-0");
    expect(dragHandle.getAttribute("aria-describedby")).toContain("task-title-task-1");

    fireEvent.click(screen.getByRole("button", { name: /Prepare release/ }));
    expect(await screen.findByRole("dialog", { name: "Prepare release" })).toBeTruthy();
    const transition = await screen.findByRole("button", { name: "Move to Specifying" });
    expect(transition).toBeTruthy();
    fireEvent.click(transition);
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/tasks/task-1/transition"),
      expect.objectContaining({ method: "POST", headers: expect.objectContaining({ "If-Match": "3" }) }),
    ));
  });

  it("searches and filters task cards by authorized stage", async () => {
    const second = { ...task, id: "task-2", title: "Audit migration", stage: "READY" as const };
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/projects")) return response([project]);
      if (url.includes("/events/stream")) return response({}, 403);
      if (url.endsWith("/tasks?limit=100")) return response([task, second]);
      if (url.endsWith("/tasks/task-1/runs") || url.endsWith("/tasks/task-2/runs")) return response([]);
      return response({}, 404);
    });
    render(<Workspace section="board" />);
    expect(await screen.findByRole("button", { name: /Prepare release/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Audit migration/ })).toBeTruthy();
    fireEvent.change(screen.getByRole("textbox", { name: "Search tasks" }), { target: { value: "audit" } });
    expect(screen.getByRole("button", { name: /Audit migration/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Prepare release/ })).toBeNull();
    fireEvent.change(screen.getByRole("textbox", { name: "Search tasks" }), { target: { value: "" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Stage" }), { target: { value: "READY" } });
    expect(screen.getByRole("button", { name: /Audit migration/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Prepare release/ })).toBeNull();
  });

  it("loads older task pages from the last visible task cursor", async () => {
    const firstPage = Array.from({ length: 100 }, (_, index) => ({
      ...task,
      id: `task-${index + 1}`,
      title: `Task ${index + 1}`,
    }));
    const older = { ...task, id: "task-101", title: "Task 101" };
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/projects")) return response([project]);
      if (url.includes("/events/stream")) return response({}, 403);
      if (url.endsWith("/tasks?limit=100")) return response(firstPage);
      if (url.endsWith("/tasks?limit=100&before=task-100")) return response([older]);
      if (url.includes("/runs")) return response([]);
      return response({}, 404);
    });

    render(<Workspace section="board" />);
    expect(await screen.findByRole("button", { name: /Task 100/ })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Load older tasks" }));
    expect(await screen.findByRole("button", { name: /Task 101/ })).toBeTruthy();
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/tasks?limit=100&before=task-100"),
      expect.objectContaining({ credentials: "include", cache: "no-store" }),
    );
  });

  it("reports task pagination failures without hiding the loaded page", async () => {
    const firstPage = Array.from({ length: 100 }, (_, index) => ({
      ...task,
      id: `task-${index + 1}`,
      title: `Task ${index + 1}`,
    }));
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/projects")) return response([project]);
      if (url.includes("/events/stream")) return response({}, 403);
      if (url.endsWith("/tasks?limit=100")) return response(firstPage);
      if (url.includes("/tasks?limit=100&before=")) return response({}, 503);
      if (url.includes("/runs")) return response([]);
      return response({}, 404);
    });

    render(<Workspace section="board" />);
    expect(await screen.findByRole("button", { name: /Task 100/ })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Load older tasks" }));
    expect(await screen.findByText("Older tasks could not be loaded. Retry after checking the connection.")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Task 100/ })).toBeTruthy();
  });

  it("renders named application routes and the durable events state", async () => {
    const sampleRun = { id: "run-1", attempt: 2, status: "FAILED", started_at: null, error_code: null };
    const sampleExperiment = { id: "experiment-1", task_id: task.id, status: "succeeded", mode: "parallel", k: 1, base_sha: "b".repeat(40), specification_hash: "c".repeat(64), policy_revision: 2, upstream_revision: "a".repeat(40), created_at: "2026-10-23T00:00:00Z", result: {} };
    const sampleRequest = { id: "request-1", request_id: "request-1", attempt: 1, event_type: "completed", project_id: project.id, task_id: task.id, run_id: "run-1", account_id: null, deployment_id: null, pool_name: "default", model_alias: "safe-alias", actual_model: null, request_identifier: null, route_decision: "primary", price_revision: null, input_tokens: 10, output_tokens: 5, usage_known: true };
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/projects")) return response([project]);
      if (url.includes("/events/stream")) return response({}, 403);
      if (url.endsWith("/tasks?limit=100")) return response([task]);
      if (url.endsWith("/tasks/task-1/runs")) return response([sampleRun]);
      if (url.endsWith("/projects/project-1/harness")) return response({ experiments: [sampleExperiment] });
      if (url.endsWith("/repositories")) return response([{ id: "repository-1", target_id: "https://github.com/acme/repo.git", display_name: "Acme repo" }]);
      if (url.endsWith("/specification-explorer")) return response({ revision: "rev-1", entries: [{ revision: "a".repeat(40), path: "openspec/specs/release/spec.md", section: "Requirements", archived: true }] });
      if (url.endsWith("/models/usage")) return response({ billed_usd: "1.25", estimated_usd: "0.25", input_tokens: 10, output_tokens: 5, unknown_usage_requests: 0, pending_reservations: 0 });
      if (url.endsWith("/models/requests?limit=50")) return response([sampleRequest]);
      if (url.endsWith("/models/budget")) return response({ project_limit_usd: "10", task_limit_usd: "5", run_limit_usd: "2", project_token_limit: 1000, task_token_limit: 500, run_token_limit: 250, max_steps: 10, max_wall_seconds: 60, max_output_tokens: 500, max_attempts: 3 });
      return response({}, 404);
    });
    const routes = [
      ["openspec", "OpenSpec"],
      ["harness", "Harness Studio"],
      ["evals", "Evals"],
      ["routing", "Routing"],
      ["events", "Events"],
    ] as const;
    for (const [section, heading] of routes) {
      const view = render(<Workspace section={section} />);
      expect(await screen.findByRole("heading", { name: heading })).toBeTruthy();
      if (section === "openspec") {
        expect(await screen.findByText("openspec/specs/release/spec.md")).toBeTruthy();
        expect(screen.getByRole("link", { name: new RegExp(`Open openspec/specs/release/spec.md at source revision ${"a".repeat(40)}`) }).getAttribute("href")).toBe(`https://github.com/acme/repo/blob/${"a".repeat(40)}/openspec/specs/release/spec.md`);
      }
      if (section === "harness") expect(await screen.findByText("Attempt 2")).toBeTruthy();
      if (section === "evals") expect(await screen.findByText("Specification READY")).toBeTruthy();
      if (section === "evals") {
        expect(await screen.findByText("Experiment experiment-1")).toBeTruthy();
        expect(screen.getByText("b".repeat(40))).toBeTruthy();
      }
      if (section === "routing") {
        expect(await screen.findByText("Billed spend")).toBeTruthy();
        expect(screen.getByText("Project budget policy")).toBeTruthy();
        expect(screen.getByText("safe-alias")).toBeTruthy();
      }
      if (section === "events") expect(await screen.findByText(/Event access was revoked/)).toBeTruthy();
      view.unmount();
    }
  });

  it("shows empty and unavailable evaluation states without inventing records", async () => {
    for (const [status, message] of [
      [403, "Evaluation evidence is not authorized for this project."],
      [503, "Evaluation evidence is unavailable."],
      [200, "No Harness² evaluation records are available for this project."],
    ] as const) {
      vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/projects")) return response([project]);
        if (url.includes("/events/stream")) return response({}, 403);
        if (url.endsWith("/tasks?limit=100")) return response([task]);
        if (url.endsWith("/projects/project-1/evaluations")) return response({ runs: [] });
        if (url.endsWith("/projects/project-1/harness")) return response(status === 200 ? { experiments: [] } : {}, status);
        if (url.includes("/runs")) return response([]);
        return response({}, 404);
      });
      const view = render(<Workspace section="evals" />);
      expect(await screen.findByText(message)).toBeTruthy();
      view.unmount();
    }
  });

  it("queues and cancels a bounded Harness² evaluation from a READY probe task", async () => {
    const probeTask = { ...task, stage: "HARNESS_PROBE" as const };
    const budget = {
      id: "budget-1", project_id: project.id,
      project_limit_usd: "1.00", task_limit_usd: "0.10", run_limit_usd: "0.05",
      probe_project_limit_usd: "0.50", probe_task_limit_usd: "0.20", probe_run_limit_usd: "0.08",
      project_token_limit: 100000, task_token_limit: 10000, run_token_limit: 5000,
      probe_project_token_limit: 50000, probe_task_token_limit: 15000, probe_run_token_limit: 7000,
      max_steps: 8, max_wall_seconds: 300, max_output_tokens: 2048, max_attempts: 2,
    };
    let experiment: Record<string, unknown> | null = null;
    let groupedRun: Record<string, unknown> | null = null;
    let failEstimate = true;
    let failGroupCancel = true;
    let failCreate = true;
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/projects")) return response([project]);
      if (url.includes("/events/stream")) return response({}, 403);
      if (url.endsWith("/tasks?limit=100")) return response([probeTask]);
      if (url.endsWith("/tasks/task-1/runs")) return response([]);
      if (url.endsWith("/projects/project-1/harness")) return response({
        experiments: experiment ? [experiment] : [],
        experiences: [{ id: "skill-1", cue: "Bounded guidance", status: "candidate", version: 1, artifact_digest: "f".repeat(64), conflict_state: "clear", license_id: "MIT" }],
        probes: experiment ? [
          { experiment_id: experiment.id, status: "succeeded", delivered: true, wall_ms: 100, cost_usd: "0.01" },
          { experiment_id: experiment.id, status: "failed", delivered: false, wall_ms: 200, cost_usd: "0.02" },
        ] : [],
      });
      if (url.endsWith("/projects/project-1/evaluations") && init?.method === "POST") {
        if (failCreate) {
          failCreate = false;
          return response({}, 409);
        }
        groupedRun = { id: "evaluation-1", stable_key: "stable-1", status: "queued", manifest: { estimate_usd: "0.04" }, manifest_digest: "d".repeat(64), sample_size: 6, completed_samples: 0, failed_samples: 0, cancelled_samples: 0, missing_samples: 6, delivery_samples: 0, delivery_successes: 0, delivery_wilson_95: null, cost_usd: null, experiment_ids: ["experiment-1"] };
        experiment = { id: "experiment-1", task_id: probeTask.id, status: "queued", mode: "both", k: 1, base_sha: "b".repeat(40), specification_hash: "c".repeat(64), policy_revision: 1, upstream_revision: "a".repeat(40), result: { adaptation_usage: { usage_known: true, cost_usd: "0.03" } } };
        return response(groupedRun, 202);
      }
      if (url.endsWith("/projects/project-1/evaluations/evaluation-1/cancel") && init?.method === "POST") {
        if (failGroupCancel) {
          failGroupCancel = false;
          return response({}, 409);
        }
        groupedRun = { ...groupedRun, status: "cancelled" };
        return response(groupedRun);
      }
      if (url.endsWith("/projects/project-1/evaluations/dry-run") && init?.method === "POST") {
        if (failEstimate) {
          failEstimate = false;
          return response({}, 409);
        }
        return response({ cost_estimate_usd: "0.04", estimate_kind: "conservative_context_window_upper_bound", manifest_digest: "d".repeat(64) });
      }
      if (url.endsWith("/projects/project-1/evaluations")) return response({ runs: groupedRun ? [groupedRun] : [] });
      if (url.endsWith("/projects/project-1/models/budget")) return response(budget);
      if (url.endsWith("/harness-experiments/experiment-1/cancel") && init?.method === "POST") {
        experiment = { ...experiment, status: "cancelled" };
        return response(experiment);
      }
      return response({}, 404);
    });

    render(<Workspace section="evals" />);
    fireEvent.click(await screen.findByRole("checkbox", { name: "Prepare release" }));
    const skillSelect = await screen.findByRole("combobox", { name: "Candidate skill version" });
    fireEvent.change(skillSelect, { target: { value: "skill-1" } });
    expect(await screen.findByText(/The estimate assumes the full route context window and configured retry bound/)).toBeTruthy();
    await waitFor(() => expect((screen.getByRole("button", { name: "Estimate evaluation cost" }) as HTMLButtonElement).disabled).toBe(false), { timeout: 3000 });
    fireEvent.click(screen.getByRole("button", { name: "Estimate evaluation cost" }));
    expect(await screen.findByText(/Configure the selected probe route and a finite project\/run budget/)).toBeTruthy();
    await waitFor(() => expect((screen.getByRole("button", { name: "Estimate evaluation cost" }) as HTMLButtonElement).disabled).toBe(false), { timeout: 3000 });
    fireEvent.click(screen.getByRole("button", { name: "Estimate evaluation cost" }));
    expect(await screen.findByText(/Conservative maximum estimate: \$0.04/)).toBeTruthy();
    fireEvent.change(screen.getByRole("combobox", { name: "Comparison mode" }), { target: { value: "parallel" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Candidate sample size" }), { target: { value: "2" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Evaluation route alias" }), { target: { value: "coding" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Comparison mode" }), { target: { value: "both" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Candidate sample size" }), { target: { value: "1" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Evaluation route alias" }), { target: { value: "harnessa-deepseek-flash" } });
    await waitFor(() => expect((screen.getByRole("button", { name: "Queue immutable evaluation" }) as HTMLButtonElement).disabled).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "Estimate evaluation cost" }));
    expect(await screen.findByText(/Conservative maximum estimate: \$0.04/)).toBeTruthy();
    await waitFor(() => expect((screen.getByRole("button", { name: "Queue immutable evaluation" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Queue immutable evaluation" }));
    expect(await screen.findByText(/task must have a frozen READY specification and be in Harness probe/)).toBeTruthy();
    await waitFor(() => expect((screen.getByRole("button", { name: "Queue immutable evaluation" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Queue immutable evaluation" }));
    expect(await screen.findByText(/Immutable evaluation evaluation-1 queued/)).toBeTruthy();
    expect(await screen.findByText(/0\/6 rollout records · 6 missing/)).toBeTruthy();
    expect(screen.getByText(/Delivery 0\/0 · uncertainty unavailable/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Cancel grouped evaluation" }));
    expect(await screen.findByText(/evaluation state or frozen inputs no longer allow this operation/)).toBeTruthy();
    await waitFor(() => expect((screen.getByRole("button", { name: "Cancel grouped evaluation" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Cancel grouped evaluation" }));
    expect(await screen.findByText(/cancellation was recorded across queued and active children/)).toBeTruthy();
    await waitFor(() => expect(groupedRun?.status).toBe("cancelled"));
    expect(await screen.findByText(/Experiment experiment-1/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Cancel evaluation" })).toBeTruthy();
    expect(screen.getByText(/Sample size 2 · failed rollouts 1 · delivery 1\/2 · 95% Wilson interval/)).toBeTruthy();
    expect(screen.getByText(/Latency p50\/p95 100\/200 ms · all-in recorded cost \$0.06000000/)).toBeTruthy();
    const createCall = vi.mocked(fetch).mock.calls.find(([input, init]) => String(input).endsWith("/evaluations") && init?.method === "POST");
    expect(createCall?.[1]?.body).toContain('"mode":"both"');
    expect(createCall?.[1]?.body).toContain('"k":1');
    expect(createCall?.[1]?.body).toContain('"skill_experience_id":"skill-1"');
    fireEvent.click(screen.getByRole("button", { name: "Cancel evaluation" }));
    expect(await screen.findByText(/Cancellation was recorded/)).toBeTruthy();
    await waitFor(() => expect(screen.getAllByText("cancelled").length).toBeGreaterThan(0));
  });

  it("resumes a failed evaluation only through its existing manifest", async () => {
    const failed = { id: "experiment-1", task_id: task.id, status: "failed", mode: "both", k: 1, base_sha: "b".repeat(40), specification_hash: "c".repeat(64), policy_revision: 1, upstream_revision: "a".repeat(40), result: { route_alias: "harnessa-deepseek-flash" } };
    let experiment: Record<string, unknown> = failed;
    let rejectResume = true;
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/projects")) return response([project]);
      if (url.includes("/events/stream")) return response({}, 403);
      if (url.endsWith("/tasks?limit=100")) return response([task]);
      if (url.endsWith("/projects/project-1/harness")) return response({ experiments: [experiment] });
      if (url.endsWith("/projects/project-1/models/budget")) return response({ project_limit_usd: "1", task_limit_usd: "1", run_limit_usd: "1", max_steps: 2, max_attempts: 1 });
      if (url.endsWith("/harness-experiments/experiment-1/resume") && init?.method === "POST") {
        if (rejectResume) {
          rejectResume = false;
          return response({}, 409);
        }
        experiment = { ...experiment, status: "queued", result: { ...failed.result, resume_count: 1 } };
        return response(experiment, 202);
      }
      return response({}, 404);
    });
    render(<Workspace section="evals" />);
    expect(await screen.findByText(/Experiment experiment-1/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Resume same manifest" }));
    expect(await screen.findByText(/Frozen inputs or policy changed, or the two-resume limit was reached/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Resume same manifest" }));
    expect(await screen.findByText(/same frozen manifest was requeued/)).toBeTruthy();
    expect(await screen.findByText("queued")).toBeTruthy();
    expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).endsWith("/harness-experiments/experiment-1/resume"))).toBe(true);
  });

  it("resumes a failed grouped evaluation against its frozen manifest", async () => {
    let groupedRun: Record<string, unknown> = {
      id: "evaluation-1", stable_key: "stable-1", status: "failed",
      manifest: { suite: "harness2-local", estimate_usd: "0.04" },
      manifest_digest: "d".repeat(64), sample_size: 6, completed_samples: 2,
      failed_samples: 1, cancelled_samples: 0, unknown_samples: 0,
      missing_samples: 4, delivery_samples: 2, delivery_successes: 1,
      delivery_wilson_95: [0.1, 0.9], cost_usd: "0.03",
      p50_latency_ms: 100, p95_latency_ms: 200, experiment_ids: ["experiment-1"],
    };
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/projects")) return response([project]);
      if (url.includes("/events/stream")) return response({}, 403);
      if (url.endsWith("/tasks?limit=100")) return response([]);
      if (url.endsWith("/projects/project-1/harness")) return response({ experiments: [], probes: [] });
      if (url.endsWith("/projects/project-1/evaluations/evaluation-1/resume") && init?.method === "POST") {
        groupedRun = { ...groupedRun, status: "queued" };
        return response(groupedRun, 202);
      }
      if (url.endsWith("/projects/project-1/evaluations")) return response({ runs: [groupedRun] });
      return response({}, 404);
    });
    render(<Workspace section="evals" />);
    expect(await screen.findByText(/95% Wilson interval 10\.0–90\.0%/)).toBeTruthy();
    expect(screen.getByText(/2\/6 rollout records · 4 missing · 1 failed/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Resume failed children" }));
    expect(await screen.findByText(/Failed children were requeued against the same frozen manifest/)).toBeTruthy();
    await waitFor(() => expect(groupedRun.status).toBe("queued"));
  });

  it("projects Harness Studio history and sends reviewed promotion and rollback decisions", async () => {
    const run = { id: "run-1", attempt: 1, status: "SUCCEEDED", started_at: "2026-10-23T00:00:00Z" };
    const harness = {
      manifest: {
        upstream_revision: "a".repeat(40),
        upstream_license: "Apache-2.0",
        components: [{ category: "system_prompt", paths: ["systemprompt.md"] }],
      },
      experiments: [{ id: "experiment-1", status: "succeeded", mode: "parallel", k: 1, task_id: task.id, base_sha: "b".repeat(40), specification_hash: "c".repeat(64), upstream_revision: "a".repeat(40), result: { composed_successful: true } }],
      probes: [{ label: "baseline-1", phase: "baseline", status: "succeeded", delivered: true, wall_ms: 100, trace_digest: "d".repeat(64), harness_digest: "e".repeat(64), component_diff: { changed: [] } }],
      experiences: [
        { id: "experience-1", cue: "Use narrow fixtures", lesson: "Keep fixture setup local.", status: "candidate", version: 1, repository_scope: "repo/demo", domain_scope: "testing", conflict_key: "fixture-style", expiry_at: "2026-12-01T00:00:00Z", evidence_refs: [{ digest: "f".repeat(64) }], candidate_digest: "1".repeat(64) },
        { id: "experience-2", cue: "Retain previous rule", lesson: "Restore the reviewed version.", status: "superseded", version: 2, repository_scope: "repo/demo", domain_scope: "testing", conflict_key: "fixture-style", expiry_at: "2026-12-01T00:00:00Z", evidence_refs: [], candidate_digest: "2".repeat(64) },
        { id: "experience-conflict", cue: "Conflicting guidance", lesson: "Needs an explicit group decision.", status: "candidate", conflict_state: "conflict", version: 1, repository_scope: "repo/demo", domain_scope: "testing", conflict_key: "conflicting-guidance", expiry_at: "2026-12-01T00:00:00Z", evidence_refs: [], candidate_digest: "3".repeat(64) },
        { id: "../skill", cue: "Path-like identifier", lesson: "Artifact link path segments must stay bounded.", status: "active", version: 1, artifact_digest: "4".repeat(64), repository_scope: "repo/demo", domain_scope: "testing", conflict_key: "path-like", expiry_at: "2026-12-01T00:00:00Z", evidence_refs: [] },
      ],
    };
    vi.spyOn(window, "prompt").mockReturnValue("Reviewed exact candidate evidence.");
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/projects")) return response([project]);
      if (url.includes("/events/stream")) return response({}, 403);
      if (url.endsWith("/tasks?limit=100")) return response([task]);
      if (url.endsWith("/tasks/task-1/runs")) return response([run]);
      if (url.endsWith("/projects/project-1/harness")) return response(harness);
      if (url.includes("/harness-experiences/") && init?.method === "POST") return response({ status: "recorded" });
      return response({}, 404);
    });

    render(<Workspace section="harness" />);

    expect(await screen.findByText(/Harness² .* Apache-2.0/)).toBeTruthy();
    expect(await screen.findByText("Experiment history")).toBeTruthy();
    expect(screen.getByText("Probe comparisons")).toBeTruthy();
    expect(screen.getByText("Use narrow fixtures")).toBeTruthy();
    expect(screen.getByText(/Evidence: .*f{64}/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Review exact skill artifact" }).getAttribute("href"))
      .toContain("%2Fskill");
    fireEvent.click(screen.getAllByRole("button", { name: "Review and promote" })[0]);
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/harness-experiences/experience-1/promote"),
      expect.objectContaining({ method: "POST", body: expect.stringContaining("Reviewed exact candidate evidence.") }),
    ));
    fireEvent.click(screen.getByRole("button", { name: "Review and roll back to this version" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/harness-experiences/experience-2/rollback"),
      expect.objectContaining({ method: "POST" }),
    ));
    fireEvent.click(screen.getByRole("button", { name: "Reject conflicting group" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/harness-experiences/experience-conflict/resolve-conflict"),
      expect.objectContaining({ method: "POST", body: expect.stringContaining("Reviewed exact candidate evidence.") }),
    ));
    expect(await screen.findByText(/Conflicting candidates were rejected and audited/)).toBeTruthy();
  });

  it("shows authenticated GitHub health and queues an administrator repair", async () => {
    const status = {
      configured_apps: [{ id: "app-1", app_id: 55, connection_status: "permission_missing", checked_at: null, last_error: "Missing Checks:write" }],
      installations: [{ id: "install-1", account_login: "harnessa-fixture", active: true, last_error: null }],
      repository_links: [{ id: "link-1", full_name: "harnessa-fixture/demo", opted_in: true, setup_status: "ready", last_error: null }],
      actor_mappings: [],
      webhook_health: { failed: 1 },
      recent_deliveries: [{ id: "delivery-row-1", delivery_id: "fixture-delivery", event_name: "issues", action: "opened", status: "failed", last_error: "handler failed" }],
    };
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/projects")) return response([project]);
      if (url.includes("/events/stream")) return response({}, 403);
      if (url.endsWith("/tasks?limit=100")) return response([task]);
      if (url.endsWith("/github/status")) return response(status);
      if (url.endsWith("/github/deliveries/delivery-row-1/retry") && init?.method === "POST") return response({ status: "queued" }, 202);
      return response({}, 404);
    });
    vi.spyOn(window, "prompt").mockReturnValue("Corrected the deployment secret reference");
    render(<Workspace section="github" />);
    expect(await screen.findByText("App 55")).toBeTruthy();
    expect(screen.getByText("harnessa-fixture/demo")).toBeTruthy();
    expect(screen.getByText("fixture-delivery")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Repair" }));
    expect(await screen.findByText("Webhook delivery queued for repair.")).toBeTruthy();
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/github/deliveries/delivery-row-1/retry"),
      expect.objectContaining({ method: "POST", body: JSON.stringify({ reason: "Corrected the deployment secret reference" }) }),
    ));
  });

  it("supports inspector focus restoration and its empty artifact and activity states", async () => {
    render(<Workspace section="board" />);
    const card = await screen.findByRole("button", { name: /Prepare release/ });
    card.focus();
    fireEvent.click(card);
    expect(await screen.findByText("No runs recorded for this task.")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Artifacts" }));
    expect(await screen.findByText("No specification interview is attached to this task.")).toBeTruthy();
    const dialog = screen.getByRole("dialog");
    const controls = dialog.querySelectorAll<HTMLElement>("button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])");
    controls[controls.length - 1]?.focus();
    fireEvent.keyDown(dialog, { key: "Tab" });
    expect(document.activeElement).toBe(controls[0]);
    fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(controls[controls.length - 1]);
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(card);
  });

  it("filters and pages provenance and records a bounded artifact integrity check", async () => {
    const run = { id: "run-1", task_id: task.id, project_id: project.id, attempt: 1, status: "SUCCEEDED", adapter_name: "fixture", fencing_token: 1, created_at: "2026-10-23T00:00:00Z" };
    const evidenceItem = {
      kind: "artifact", summary: "text/plain; 4 bytes", import_status: "native", reconciliation_status: "checksum_unverified", stale: false,
      envelope: {
        source_system: "harnessa.artifact_store", source_record_id: { status: "known", value: "record-1" },
        recorded_at: "2026-10-23T00:00:00Z", revision: { status: "known", value: "1" },
        content_sha256: { status: "known", value: "a".repeat(64) }, artifact_id: { status: "known", value: "artifact-1" },
      },
    };
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/projects")) return response([project]);
      if (url.includes("/events/stream")) return response({}, 403);
      if (url.endsWith("/tasks?limit=100")) return response([task]);
      if (url.endsWith("/tasks/task-1/runs")) return response([run]);
      if (url.endsWith("/tasks/task-1/capabilities")) return response({ allowed_commands: [], allowed_transitions: [] });
      if (url.endsWith("/tasks/task-1/specification-interview")) return response({}, 404);
      if (url.includes("/evidence/artifacts/artifact-1/reconcile") && init?.method === "POST") return response({ status: "verified" });
      if (url.includes("/evidence?")) {
        if (!url.includes("cursor=") && !url.includes("source=")) return response({ items: [evidenceItem], next_cursor: "cursor-next", limit: 100 });
        return response({ items: [{ ...evidenceItem, kind: "model_request", envelope: { ...evidenceItem.envelope, source_system: "litellm.gateway_ledger", artifact_id: { status: "unknown" } } }], next_cursor: null, limit: 100 });
      }
      return response({}, 404);
    });
    render(<Workspace section="board" />);
    fireEvent.click(await screen.findByRole("button", { name: /Prepare release/ }));
    fireEvent.click(await screen.findByRole("tab", { name: "Provenance" }));
    expect(await screen.findByText("text/plain; 4 bytes")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Check artifact SHA-256" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/evidence/artifacts/artifact-1/reconcile"),
      expect.objectContaining({ method: "POST" }),
    ));
    fireEvent.click(await screen.findByRole("button", { name: "Load more evidence" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("cursor=cursor-next"),
      expect.anything(),
    ));
    fireEvent.change(screen.getByLabelText("Run"), { target: { value: "run-1" } });
    fireEvent.change(screen.getByLabelText("Source"), { target: { value: "litellm.gateway_ledger" } });
    fireEvent.change(screen.getByLabelText("Revision or SHA"), { target: { value: "rev-7" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringMatching(/run_id=run-1.*source=litellm.gateway_ledger.*revision=rev-7/),
      expect.anything(),
    ));
  });

  it("renders denied commands and submits an authorized specification answer", async () => {
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/projects")) return response([project]);
      if (url.includes("/events/stream")) return response({}, 403);
      if (url.endsWith("/tasks?limit=100")) return response([task]);
      if (url.endsWith("/tasks/task-1/runs")) return response([]);
      if (url.endsWith("/tasks/task-1/capabilities")) return response({ allowed_commands: ["cancel"], allowed_transitions: [], can_answer_questions: true, can_edit_artifacts: false });
      if (url.endsWith("/tasks/task-1/specification-interview")) return response(interview);
      if (url.endsWith("/questions")) return response([question]);
      if (url.endsWith("/commands")) return response({}, 403);
      if (url.endsWith("/answers") && init?.method === "POST") return response({ interview: { ...interview, revision: 2 } });
      if (url.endsWith("/tasks/task-1")) return response(task);
      return response({}, 404);
    });
    render(<Workspace section="board" />);
    fireEvent.click(await screen.findByRole("button", { name: /Prepare release/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Cancel run" }));
    expect(await screen.findByText("Your project role cannot cancel this run.")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Interview" }));
    const answer = await screen.findByLabelText("Answer");
    fireEvent.change(answer, { target: { value: "Only project owners approve." } });
    fireEvent.click(screen.getByRole("button", { name: "Submit answer" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/answers"),
      expect.objectContaining({ method: "POST", body: expect.stringContaining("Only project owners approve.") }),
    ));
    expect(await screen.findByText("Answer recorded with an idempotency key.")).toBeTruthy();
  });

  it("loads hostile Markdown as sanitized preview and exposes only the authorized artifact save", async () => {
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/projects")) return response([project]);
      if (url.includes("/events/stream")) return response({}, 403);
      if (url.endsWith("/tasks?limit=100")) return response([task]);
      if (url.endsWith("/tasks/task-1/runs")) return response([]);
      if (url.endsWith("/tasks/task-1/capabilities")) return response({ allowed_commands: [], allowed_transitions: [], can_answer_questions: false, can_edit_artifacts: true });
      if (url.endsWith("/tasks/task-1/specification-interview")) return response(interview);
      if (url.endsWith("/questions")) return response([]);
      if (url.endsWith("/artifacts") && init?.method === "PUT") return response({ ...interview, revision: 2 });
      if (url.endsWith("/artifacts") && !init?.method) return response(artifacts);
      if (url.endsWith("/tasks/task-1")) return response(task);
      return response({}, 404);
    });
    render(<Workspace section="board" />);
    fireEvent.click(await screen.findByRole("button", { name: /Prepare release/ }));
    expect(await screen.findByText("Spec DRAFT · r1")).toBeTruthy();
    fireEvent.click(await screen.findByRole("tab", { name: "Artifacts" }));
    await screen.findByLabelText("OpenSpec artifact");
    fireEvent.click(screen.getByText("Safe Markdown preview"));
    expect(await screen.findByText("Safe text")).toBeTruthy();
    expect(document.querySelector("script")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Save artifact revision" }));
    expect(await screen.findByText("Artifact revision saved. Readiness must be evaluated again.")).toBeTruthy();
  });

  it("resumes durable events, suppresses duplicates, and hides protocol content", async () => {
    sessionStorage.setItem("harnessa.cursor.project-1", "3");
    let streamRequests = 0;
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/projects")) return response([project]);
      if (url.endsWith("/tasks?limit=100")) return response([task]);
      if (url.includes("/events/stream")) {
        streamRequests += 1;
        if (streamRequests > 1) return response({}, 403);
        const frames = [
          'id: 4\ndata: {"type":"TEXT_MESSAGE_CONTENT","messageId":"private-id","delta":"private assistant content"}\n\n',
          'id: 4\ndata: {"type":"RUN_STARTED","threadId":"private-thread","runId":"private-run"}\n\n',
          'id: 5\ndata: {"type":"FUTURE_EVENT","payload":"must not render"}\n\n',
          'id: 6\ndata: {broken json}\n\n',
          ...[
            [{ type: "RUN_STARTED", threadId: "t", runId: "r" }, "Run started"],
            [{ type: "RUN_FINISHED", threadId: "t", runId: "r" }, "Run finished"],
            [{ type: "RUN_ERROR", message: "private error", code: "failure" }, "Run failed"],
            [{ type: "STEP_STARTED", stepName: "private step" }, "Workflow step started"],
            [{ type: "STEP_FINISHED", stepName: "private step" }, "Workflow step finished"],
            [{ type: "TOOL_CALL_START", toolCallId: "private-tool", toolCallName: "read" }, "Tool started"],
            [{ type: "TOOL_CALL_END", toolCallId: "private-tool" }, "Tool finished"],
            [{ type: "STATE_SNAPSHOT", snapshot: { stage: "DONE" } }, "Task state updated"],
            [{ type: "STATE_DELTA", delta: [] }, "Task progress updated"],
            [{ type: "CUSTOM", name: "harnessa.domain_event", value: { private: true } }, "Workspace update"],
            [{ type: "TEXT_MESSAGE_START", messageId: "private-message", role: "assistant" }, "Workspace update"],
            ...Array.from({ length: 20 }, (_, index) => [{ type: "RUN_STARTED", threadId: "t", runId: `r-${index}` }, "Run started"]),
          ].map(([event], index) => `id: ${index + 7}\ndata: ${JSON.stringify(event)}\n\n`),
        ].join("");
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(new TextEncoder().encode(frames));
            controller.close();
          },
        });
        return { ok: true, status: 200, body } as Response;
      }
      return response({}, 404);
    });
    render(<Workspace section="events" />);
    expect(await screen.findByText("Assistant update")).toBeTruthy();
    await screen.findByText("Reconnect resumes persisted events; it does not start or cancel a run.");
    expect(screen.queryByText(/Resume cursor|Update \d+/)).toBeNull();
    for (const summary of ["Run started", "Run finished", "Run failed", "Workflow step started", "Workflow step finished", "Tool started", "Tool finished", "Task state updated", "Task progress updated", "Workspace update"]) {
      expect(screen.getAllByText(summary).length).toBeGreaterThan(0);
    }
    expect(screen.queryByText("private assistant content")).toBeNull();
    expect(screen.queryByText("must not render")).toBeNull();
    fireEvent.change(screen.getByRole("textbox", { name: "Search events" }), { target: { value: "Run finished" } });
    expect(screen.getByText("Run finished")).toBeTruthy();
    expect(screen.queryByText("Run failed")).toBeNull();
    fireEvent.change(screen.getByRole("textbox", { name: "Search events" }), { target: { value: "" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Filter event type" }), { target: { value: "RUN_FINISHED" } });
    expect(screen.getByText("Run finished")).toBeTruthy();
    expect(screen.queryByText("Run started")).toBeNull();
    fireEvent.change(screen.getByRole("combobox", { name: "Filter event type" }), { target: { value: "all" } });
    fireEvent.click(screen.getByRole("button", { name: "Load 25 more events" }));
    expect(await screen.findByText("RUN_STARTED · cursor 36")).toBeTruthy();
  });

  it("shows project denial and refreshes an optimistic transition conflict", async () => {
    let boardLoads = 0;
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/projects")) return response([project]);
      if (url.includes("/events/stream")) return response({}, 403);
      if (url.endsWith("/tasks?limit=100")) {
        boardLoads += 1;
        return response([boardLoads === 1 ? task : { ...task, version: 4, stage: "SPECIFYING" }]);
      }
      if (url.endsWith("/tasks/task-1/runs")) return response([]);
      if (url.endsWith("/tasks/task-1/capabilities")) return response({ allowed_commands: [], allowed_transitions: ["SPECIFYING"] });
      if (url.endsWith("/tasks/task-1/specification-interview")) return response({}, 404);
      if (url.endsWith("/tasks/task-1/transition") && init?.method === "POST") return response({}, 409);
      if (url.endsWith("/tasks/task-1")) return response({ ...task, version: 4, stage: "SPECIFYING" });
      return response({}, 404);
    });
    render(<Workspace section="board" />);
    fireEvent.click(await screen.findByRole("button", { name: /Prepare release/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Move to Specifying" }));
    expect(await screen.findByText("The task changed in another session. The board has been refreshed.")).toBeTruthy();
    await waitFor(() => expect(screen.getByText("v4")).toBeTruthy());

    cleanup();
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/projects")) return response([project]);
      if (url.includes("/events/stream")) return response({}, 403);
      if (url.endsWith("/tasks?limit=100")) return response({}, 403);
      return response({}, 404);
    });
    render(<Workspace section="board" />);
    expect(await screen.findByText("Access to this project was denied.")).toBeTruthy();
  });

  it("recovers an expired cursor from a task snapshot before resuming the stream", async () => {
    let streamRequests = 0;
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/projects")) return response([project]);
      if (url.endsWith("/tasks?limit=100")) return response([task]);
      if (url.includes("/events/stream")) {
        streamRequests += 1;
        if (streamRequests === 1) return response({ detail: { oldest_available_cursor: 10 } }, 410);
        if (streamRequests > 2) return response({}, 403);
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(new TextEncoder().encode(
              'id: 11\ndata: {"type":"STATE_SNAPSHOT","snapshot":{"stage":"READY"}}\n\n',
            ));
            controller.close();
          },
        });
        return { ok: true, status: 200, body } as Response;
      }
      return response({}, 404);
    });
    render(<Workspace section="events" />);
    expect(await screen.findByText("Task state updated")).toBeTruthy();
    await screen.findByText("Reconnect resumes persisted events; it does not start or cancel a run.");
    expect(screen.queryByText(/Resume cursor|Update \d+/)).toBeNull();
    expect(sessionStorage.getItem("harnessa.cursor.project-1")).toBe("11");
    expect(streamRequests).toBeGreaterThanOrEqual(2);
  });

  it("creates a task, toggles the theme, and signs out without exposing credentials", async () => {
    let created = false;
    let createRequests = 0;
    vi.spyOn(window, "prompt").mockReturnValue("Follow-up task");
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/projects")) return response([project]);
      if (url.includes("/events/stream")) return response({}, 403);
      if (url.endsWith("/tasks?limit=100")) return response(created ? [task, { ...task, id: "task-2", title: "Follow-up task" }] : [task]);
      if (url.endsWith("/projects/project-1/tasks") && init?.method === "POST") {
        createRequests += 1;
        if (createRequests > 1) return response({}, 403);
        created = true;
        return response({ ...task, id: "task-2", title: "Follow-up task" }, 201);
      }
      if (url.endsWith("/auth/logout")) return response({}, 204);
      return response({}, 404);
    });
    render(<Workspace section="board" />);
    fireEvent.click(await screen.findByRole("button", { name: "Use dark theme" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
    fireEvent.click(screen.getByRole("button", { name: "Create task" }));
    expect(await screen.findByRole("button", { name: /Follow-up task/ })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Create task" }));
    expect(await screen.findByText("Task creation is not authorized.")).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "Sign out" }).at(-1)!);
    await waitFor(() => expect(push).toHaveBeenCalledWith("/login"));
  });

  it("shows authorized failed-run details and refreshes after a successful cancel command", async () => {
    const run = { id: "run-1", attempt: 1, status: "FAILED", started_at: null, error_code: "worker_error" };
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/projects")) return response([project]);
      if (url.includes("/events/stream")) return response({}, 403);
      if (url.endsWith("/tasks?limit=100")) return response([task]);
      if (url.endsWith("/tasks/task-1/runs")) return response([run]);
      if (url.endsWith("/tasks/task-1/capabilities")) return response({ allowed_commands: ["cancel"], allowed_transitions: [] });
      if (url.endsWith("/tasks/task-1/specification-interview")) return response({}, 404);
      if (url.endsWith("/commands") && init?.method === "POST") return response({ status: "accepted" }, 202);
      if (url.endsWith("/tasks/task-1")) return response(task);
      return response({}, 404);
    });
    render(<Workspace section="board" />);
    fireEvent.click(await screen.findByRole("button", { name: /Prepare release/ }));
    expect(await screen.findByText("Run 1 · FAILED")).toBeTruthy();
    expect(screen.getByText("This run failed. Review authorized diagnostics for details.")).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: "Cancel run" }));
    expect(await screen.findByText("Task cancel command accepted.")).toBeTruthy();
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/tasks/task-1/commands"),
      expect.objectContaining({ method: "POST", headers: expect.objectContaining({ "If-Match": "3" }) }),
    ));
  });

  it("reconciles a lost transition response and reports OpenSpec access denial", async () => {
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/projects")) return response([project]);
      if (url.includes("/events/stream")) return response({}, 403);
      if (url.endsWith("/tasks?limit=100")) return response([task]);
      if (url.endsWith("/tasks/task-1/runs")) return response([]);
      if (url.endsWith("/tasks/task-1/capabilities")) return response({ allowed_commands: [], allowed_transitions: ["SPECIFYING"] });
      if (url.endsWith("/tasks/task-1/specification-interview")) return response({}, 404);
      if (url.endsWith("/tasks/task-1/transition") && init?.method === "POST") throw new Error("connection dropped");
      if (url.endsWith("/repositories")) return response([{ id: "repository-1" }]);
      if (url.endsWith("/specification-explorer")) return response({}, 403);
      return response({}, 404);
    });
    render(<Workspace section="board" />);
    fireEvent.click(await screen.findByRole("button", { name: /Prepare release/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Move to Specifying" }));
    expect(await screen.findByText(/The transition response was lost/)).toBeTruthy();
    cleanup();
    render(<Workspace section="openspec" />);
    expect(await screen.findByText("OpenSpec access denied.")).toBeTruthy();
  });

  it("renders the no-authorized-project state without exposing other workspace routes", async () => {
    vi.mocked(fetch).mockResolvedValue(response([]));
    render(<Workspace section="board" />);
    expect(await screen.findByRole("heading", { name: "No project selected" })).toBeTruthy();
    expect(screen.getByRole("option", { name: "No authorized projects" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await screen.findByText("Sign in with a project membership or create a project from setup.")).toBeTruthy();
  });

  it("reports project-list authorization denial", async () => {
    let attempts = 0;
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).endsWith("/projects")) return response({}, ++attempts === 1 ? 403 : 500);
      return response({}, 404);
    });
    const first = render(<Workspace section="board" />);
    expect(await screen.findByText("You do not have access to projects.")).toBeTruthy();
    first.unmount();
    render(<Workspace section="board" />);
    expect(await screen.findByText("Project list is unavailable.")).toBeTruthy();
  });

  it("redirects expired sessions from project and task loads", async () => {
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => {
      return String(input).endsWith("/projects") ? response({}, 401) : response({}, 404);
    });
    const first = render(<Workspace section="board" />);
    await waitFor(() => expect(push).toHaveBeenCalledWith("/login"));
    first.unmount();
    push.mockReset();
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/projects")) return response([project]);
      if (url.endsWith("/tasks?limit=100")) return response({}, 401);
      return response({}, 404);
    });
    render(<Workspace section="board" />);
    await waitFor(() => expect(push).toHaveBeenCalledWith("/login"));
  });

  it("reports denied artifact access and offers a bounded retry", async () => {
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/projects")) return response([project]);
      if (url.includes("/events/stream")) return response({}, 403);
      if (url.endsWith("/tasks?limit=100")) return response([task]);
      if (url.endsWith("/tasks/task-1/runs")) return response([]);
      if (url.endsWith("/tasks/task-1/capabilities")) return response({ allowed_commands: [], allowed_transitions: [] });
      if (url.endsWith("/tasks/task-1/specification-interview")) return response(interview);
      if (url.endsWith("/questions")) return response([]);
      if (url.endsWith("/artifacts")) return response({}, 403);
      return response({}, 404);
    });
    render(<Workspace section="board" />);
    fireEvent.click(await screen.findByRole("button", { name: /Prepare release/ }));
    expect(await screen.findByText("Spec DRAFT · r1")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Artifacts" }));
    expect(await screen.findByText("Specification artifacts are not authorized for this account.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry artifact load" }));
    expect(await screen.findByText("Specification artifacts are not authorized for this account.")).toBeTruthy();
  });

  it("falls back to the board route for an unknown workspace path", async () => {
    render(<Workspace section="unknown" />);
    expect(await screen.findByRole("heading", { name: "Task board" })).toBeTruthy();
  });
});
