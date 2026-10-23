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
      if (url.endsWith("/tasks?limit=500")) return response([task]);
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

  it("renders named application routes and the durable events state", async () => {
    const sampleRun = { id: "run-1", attempt: 2, status: "FAILED", started_at: null, error_code: null };
    const sampleRequest = { id: "request-1", request_id: "request-1", attempt: 1, event_type: "completed", project_id: project.id, task_id: task.id, run_id: "run-1", account_id: null, deployment_id: null, pool_name: "default", model_alias: "safe-alias", actual_model: null, request_identifier: null, route_decision: "primary", price_revision: null, input_tokens: 10, output_tokens: 5, usage_known: true };
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/projects")) return response([project]);
      if (url.includes("/events/stream")) return response({}, 403);
      if (url.endsWith("/tasks?limit=500")) return response([task]);
      if (url.endsWith("/tasks/task-1/runs")) return response([sampleRun]);
      if (url.endsWith("/repositories")) return response([{ id: "repository-1" }]);
      if (url.endsWith("/specification-explorer")) return response({ revision: "rev-1", entries: [{ revision: "rev-1", path: "openspec/specs/release/spec.md", section: "Requirements", archived: true }] });
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
      if (section === "openspec") expect(await screen.findByText("openspec/specs/release/spec.md")).toBeTruthy();
      if (section === "harness") expect(await screen.findByText("Attempt 2")).toBeTruthy();
      if (section === "evals") expect(await screen.findByText("Specification READY")).toBeTruthy();
      if (section === "routing") {
        expect(await screen.findByText("Billed spend")).toBeTruthy();
        expect(screen.getByText("Project budget policy")).toBeTruthy();
        expect(screen.getByText("safe-alias")).toBeTruthy();
      }
      if (section === "events") expect(await screen.findByText(/Event access was revoked/)).toBeTruthy();
      view.unmount();
    }
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

  it("renders denied commands and submits an authorized specification answer", async () => {
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/projects")) return response([project]);
      if (url.includes("/events/stream")) return response({}, 403);
      if (url.endsWith("/tasks?limit=500")) return response([task]);
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
      if (url.endsWith("/tasks?limit=500")) return response([task]);
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
      if (url.endsWith("/tasks?limit=500")) return response([task]);
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
  });

  it("shows project denial and refreshes an optimistic transition conflict", async () => {
    let boardLoads = 0;
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/projects")) return response([project]);
      if (url.includes("/events/stream")) return response({}, 403);
      if (url.endsWith("/tasks?limit=500")) {
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
      if (url.endsWith("/tasks?limit=500")) return response({}, 403);
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
      if (url.endsWith("/tasks?limit=500")) return response([task]);
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
      if (url.endsWith("/tasks?limit=500")) return response(created ? [task, { ...task, id: "task-2", title: "Follow-up task" }] : [task]);
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
      if (url.endsWith("/tasks?limit=500")) return response([task]);
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
      if (url.endsWith("/tasks?limit=500")) return response([task]);
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
      if (url.endsWith("/tasks?limit=500")) return response({}, 401);
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
      if (url.endsWith("/tasks?limit=500")) return response([task]);
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
