import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Home from "../app/page";
import Login from "../app/login/page";
import RootLayout from "../app/layout";
import Projects from "../app/projects/page";
import SetupPage from "../app/setup/page";
import { Button } from "@harnessa/ui-react";

const { push, router } = vi.hoisted(() => {
  const push = vi.fn();
  return { push, router: { push } };
});
vi.mock("next/navigation", () => ({ useRouter: () => router }));

function response(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  };
}

describe("web application pages", () => {
  beforeEach(() => {
    push.mockReset();
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("shows the public workspace entry point", () => {
    render(<Home />);
    expect(screen.getByRole("heading", { name: "Harnessa workspace" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Open projects" }).getAttribute("href")).toBe("/projects");
  });

  it("renders the root layout and shared button variants", () => {
    const layout = RootLayout({ children: <main>Workspace</main> });
    expect(layout.props.lang).toBe("en");
    render(<Button variant="outline" size="sm" className="px-8" aria-label="Save">Save</Button>);
    const button = screen.getByRole("button", { name: "Save" });
    expect(button.className).toContain("border-input");
    expect(button.className).toContain("px-8");
  });

  it("checks setup prerequisites without sending a model request", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/auth/me")) return response({ id: "user-1", email: "admin@example.test", organization_id: "org-1", role: "administrator" }) as Response;
      if (url.endsWith("/projects")) return response([{ id: "project-1", name: "Alpha", slug: "alpha" }]) as Response;
      if (url.endsWith("/ops/models/doctor")) return response({ status: "ready", gateway_ready: true, configured_accounts: 1, configured_deployments: 1, configured_aliases: 1, pools: ["primary", "probe"], findings: [] }) as Response;
      if (url.endsWith("/github/status")) return response({ configured_apps: [], installations: [], repository_links: [], actor_mappings: [], webhook_health: {}, recent_deliveries: [] }) as Response;
      if (url.endsWith("/health/ready")) return response({ status: "ready" }) as Response;
      if (url.endsWith("/projects/project-1/repositories")) return response([{ id: "repo-1" }]) as Response;
      if (url.endsWith("/projects/project-1/execution-adapter")) return response({ project_id: "project-1", adapter: "deployment-default", available_adapters: ["deployment-default", "disabled"], configured: false, detail: "Deployment runner unavailable." }) as Response;
      return response({}, 404) as Response;
    });
    render(<SetupPage />);
    expect(await screen.findByText("admin@example.test · administrator")).toBeTruthy();
    expect(await screen.findByText("1 repository grant configured for the selected project.")).toBeTruthy();
    expect(await screen.findByText(/Provider credentials still require an authorized live probe/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Run one-token route check" }).hasAttribute("disabled")).toBe(true);
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/models/live-check"))).toBe(false);
  });

  it("lets an authorized operator configure finite project, task, and run budgets", async () => {
    const budget = {
      id: "budget-1", project_id: "project-1",
      project_limit_usd: "1.00", task_limit_usd: "0.25", run_limit_usd: "0.10",
      probe_project_limit_usd: "0.50", probe_task_limit_usd: "0.20", probe_run_limit_usd: "0.08",
      project_token_limit: 100000, task_token_limit: 25000, run_token_limit: 10000,
      probe_project_token_limit: 50000, probe_task_token_limit: 15000, probe_run_token_limit: 7000,
      max_steps: 8, max_wall_seconds: 600, max_output_tokens: 4096, max_attempts: 2,
    };
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/auth/me")) return response({ id: "user-1", email: "admin@example.test", organization_id: "org-1", role: "administrator" }) as Response;
      if (url.endsWith("/projects")) return response([{ id: "project-1", name: "Alpha", slug: "alpha" }]) as Response;
      if (url.endsWith("/ops/models/doctor")) return response({ status: "setup_required", gateway_ready: false, configured_accounts: 0, configured_deployments: 0, configured_aliases: 0, pools: [], findings: [] }) as Response;
      if (url.endsWith("/github/status")) return response({ configured_apps: [], installations: [], repository_links: [], actor_mappings: [], webhook_health: {}, recent_deliveries: [] }) as Response;
      if (url.endsWith("/health/ready")) return response({ status: "ready" }) as Response;
      if (url.endsWith("/projects/project-1/repositories")) return response([]) as Response;
      if (url.endsWith("/projects/project-1/execution-adapter")) return response({ project_id: "project-1", adapter: "deployment-default", available_adapters: ["deployment-default", "disabled"], configured: false, detail: "Deployment runner unavailable." }) as Response;
      if (url.endsWith("/projects/project-1/models/budget") && init?.method === "PUT") return response(budget) as Response;
      if (url.endsWith("/projects/project-1/models/budget")) return response({}, 404) as Response;
      return response({}, 404) as Response;
    });

    render(<SetupPage />);
    await screen.findByRole("button", { name: "Set budget policy" });
    for (const [name, value] of Object.entries({
      "project limit usd": "1.00", "task limit usd": "0.25", "run limit usd": "0.10",
      "probe project limit usd": "0.50", "probe task limit usd": "0.20", "probe run limit usd": "0.08",
      "project token limit": "100000", "task token limit": "25000", "run token limit": "10000",
      "probe project token limit": "50000", "probe task token limit": "15000", "probe run token limit": "7000",
      "max steps": "8", "max wall seconds": "600", "max output tokens": "4096", "max attempts": "2",
    })) {
      fireEvent.change(screen.getByRole("spinbutton", { name }), { target: { value } });
    }
    fireEvent.click(screen.getByRole("button", { name: "Set budget policy" }));
    expect(await screen.findByText(/Separate interactive and probe project, task, and run ceilings saved/)).toBeTruthy();
    const save = fetchMock.mock.calls.find(([input, init]) => String(input).endsWith("/models/budget") && init?.method === "PUT");
    expect(save?.[1]?.body).toContain('"project_limit_usd":"1.00"');
    expect(save?.[1]?.body).toContain('"max_steps":8');
    expect(save?.[1]?.headers).toHaveProperty("X-CSRF-Token");
  });

  it("sends only an explicitly authorized one-token route check", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/auth/me")) return response({ id: "user-1", email: "admin@example.test", organization_id: "org-1", role: "administrator" }) as Response;
      if (url.endsWith("/projects")) return response([{ id: "project-1", name: "Alpha", slug: "alpha" }]) as Response;
      if (url.endsWith("/ops/models/doctor")) return response({ status: "ready", gateway_ready: true, configured_accounts: 1, configured_deployments: 1, configured_aliases: 1, pools: ["primary", "probe"], findings: [] }) as Response;
      if (url.endsWith("/github/status")) return response({ configured_apps: [], installations: [], repository_links: [], actor_mappings: [], webhook_health: {}, recent_deliveries: [] }) as Response;
      if (url.endsWith("/health/ready")) return response({ status: "ready" }) as Response;
      if (url.endsWith("/projects/project-1/repositories")) return response([{ id: "repo-1" }]) as Response;
      if (url.endsWith("/models/live-check") && init?.method === "POST") return response({ outcome: "passed", request_id: "request-1", deployment: "deepseek", provider_account: "provider-1", actual_model: "deepseek-flash", usage_known: true, billed_usd: "0.00001", estimated_usd: "0.00001" }) as Response;
      return response({}, 404) as Response;
    });
    render(<SetupPage />);
    const check = screen.getByRole("button", { name: "Run one-token route check" });
    await screen.findByText("admin@example.test · administrator");
    expect(check.hasAttribute("disabled")).toBe(true);
    fireEvent.click(screen.getByRole("checkbox", { name: /I authorize one paid route check/ }));
    fireEvent.click(check);
    expect(await screen.findByText("Passed · deepseek · billed 0.00001 · usage known")).toBeTruthy();
    const call = fetchMock.mock.calls.find(([input]) => String(input).endsWith("/models/live-check"));
    expect(call?.[1]?.method).toBe("POST");
    expect(call?.[1]?.body).toContain('"authorize_paid_request":true');
    expect(call?.[1]?.body).toContain('"alias":"harnessa-deepseek-flash"');
  });

  it("fails closed when setup identity, route, and runtime checks are unavailable", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/auth/me")) return response({}, 401) as Response;
      if (url.endsWith("/projects")) return response([]) as Response;
      if (url.endsWith("/ops/models/doctor")) return response({ status: "setup_required", gateway_ready: false, configured_accounts: 0, configured_deployments: 0, configured_aliases: 0, pools: [], findings: ["No compatible route is registered."] }) as Response;
      if (url.endsWith("/github/status")) return response({ configured_apps: [{ connection_status: "ready" }], installations: [], repository_links: [], actor_mappings: [], webhook_health: {}, recent_deliveries: [] }) as Response;
      if (url.endsWith("/health/ready")) return response({}, 503) as Response;
      return response({}, 404) as Response;
    });

    render(<SetupPage />);
    expect(await screen.findByText("No compatible route is registered.")).toBeTruthy();
    expect(await screen.findByText("GitHub App and active installation are not fully configured. Repository mutations remain unavailable.")).toBeTruthy();
    expect(await screen.findByText("API dependency readiness could not be verified.")).toBeTruthy();
    expect(await screen.findByText("Some administrator-only setup checks are unavailable. Confirm your role and refresh the page.")).toBeTruthy();
    expect(push).toHaveBeenCalledWith("/login");
    expect(screen.getByRole("button", { name: "Run one-token route check" }).hasAttribute("disabled")).toBe(true);
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/models/live-check"))).toBe(false);
  });

  it("redirects unauthorized project loads and reports server and network failures", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(response({}, 401) as Response);
    const { unmount } = render(<Projects />);
    await waitFor(() => expect(push).toHaveBeenCalledWith("/login"));
    unmount();

    fetchMock.mockReset();
    fetchMock.mockResolvedValueOnce(response({}, 503) as Response);
    const second = render(<Projects />);
    expect(await screen.findByText("Could not load projects (503).")).toBeTruthy();
    second.unmount();

    fetchMock.mockReset();
    fetchMock.mockRejectedValueOnce(new Error("offline fixture"));
    render(<Projects />);
    expect(await screen.findByText("Could not reach the Harnessa API. Check the connection and try again.")).toBeTruthy();
  });

  it("ignores project-list responses after the page unmounts", async () => {
    const fetchMock = vi.mocked(fetch);
    let resolveResponse!: (value: Response) => void;
    const pendingResponse = new Promise<Response>((resolve) => { resolveResponse = resolve; });
    fetchMock.mockReturnValueOnce(pendingResponse);
    const { unmount } = render(<Projects />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    unmount();
    resolveResponse(response({}, 401) as Response);
    await pendingResponse;
    await Promise.resolve();
    expect(push).not.toHaveBeenCalled();
  });

  it("reports failed project creation and repository registration", async () => {
    const fetchMock = vi.mocked(fetch);
    const project = { id: "project-1", name: "Alpha", slug: "alpha" };
    fetchMock.mockImplementation(async (input, init) => {
      if (init?.method === "POST") {
        return response({}, String(input).endsWith("/repositories") ? 403 : 500) as Response;
      }
      return response([project]) as Response;
    });
    render(<Projects />);
    await screen.findByRole("button", { name: /Alpha/ });
    expect(screen.getByRole("heading", { name: "Your projects", level: 2 })).toBeTruthy();
    expect(screen.getByLabelText("Project name")).toBeTruthy();
    expect(screen.getByLabelText("Project slug")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Project name"), { target: { value: "Alpha" } });
    fireEvent.change(screen.getByLabelText("Project slug"), { target: { value: "alpha" } });
    fireEvent.submit(screen.getByRole("heading", { name: "Create project" }).closest("form")!);
    expect(await screen.findByText("Could not create project (500).")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /Alpha/ }));
    fireEvent.change(screen.getByLabelText("Repository target ID"), {
      target: { value: "team/alpha" },
    });
    fireEvent.change(screen.getByLabelText("Display name"), {
      target: { value: "Alpha repository" },
    });
    fireEvent.submit(
      screen.getByRole("heading", { name: "Register configured target" }).closest("form")!,
    );
    expect(
      await screen.findByText(/Repository registration was denied/),
    ).toBeTruthy();
  });

  it("reports rejected credentials and stores the CSRF token after login", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(response({ csrf_token: "fixture-token" }) as Response);
    fetchMock.mockResolvedValueOnce(response({}, 401) as Response);
    const { rerender } = render(<Login />);
    expect(screen.getByLabelText("Email").className).toContain("bg-background");
    expect(screen.getByRole("button", { name: "Sign in" }).className).toContain("bg-primary");
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "person@example.test" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "not-a-password" } });
    fireEvent.submit(screen.getByRole("button", { name: "Sign in" }).closest("form")!);
    expect((await screen.findByRole("alert")).textContent).toContain("Sign in failed");

    fetchMock.mockReset();
    fetchMock.mockResolvedValueOnce(response({ csrf_token: "approved-token" }) as Response);
    fetchMock.mockResolvedValueOnce(response({}) as Response);
    rerender(<Login />);
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "person@example.test" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "valid-password" } });
    fireEvent.submit(screen.getByRole("button", { name: "Sign in" }).closest("form")!);
    await waitFor(() => expect(push).toHaveBeenCalledWith("/projects"));
    expect(sessionStorage.getItem("csrf_token")).toBe("approved-token");
  });

  it("reports sign-in service failures before submitting credentials", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(response({}, 503) as Response);
    render(<Login />);
    fireEvent.submit(screen.getByRole("button", { name: "Sign in" }).closest("form")!);
    expect((await screen.findByRole("alert")).textContent).toContain("unavailable (503)");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("reports project, repository, and sign-out service failures", async () => {
    const fetchMock = vi.mocked(fetch);
    const project = { id: "project-1", name: "Alpha", slug: "alpha" };
    let createRequests = 0;
    let repositoryRequests = 0;
    let signOutRequests = 0;
    fetchMock.mockImplementation((input, init) => {
      const url = String(input);
      if (url.endsWith("/projects") && init?.method === "POST") {
        createRequests += 1;
        return createRequests === 1
          ? Promise.resolve(response({}, 409) as Response)
          : Promise.reject("offline fixture");
      }
      if (url.endsWith("/projects")) return Promise.resolve(response([project]) as Response);
      if (url.endsWith("/project-1/repositories")) {
        repositoryRequests += 1;
        return repositoryRequests === 1
          ? Promise.resolve(response({}, 403) as Response)
          : Promise.reject("offline fixture");
      }
      if (url.endsWith("/auth/logout")) {
        signOutRequests += 1;
        return signOutRequests === 1
          ? Promise.resolve(response({}, 403) as Response)
          : Promise.reject("offline fixture");
      }
      return Promise.resolve(response({}, 404) as Response);
    });

    render(<Projects />);
    await screen.findByRole("button", { name: /Alpha/ });
    fireEvent.change(screen.getByLabelText("Project name"), { target: { value: "Alpha" } });
    fireEvent.change(screen.getByLabelText("Project slug"), { target: { value: "alpha" } });
    const projectForm = screen.getByRole("heading", { name: "Create project" }).closest("form")!;

    fireEvent.submit(projectForm);
    expect(await screen.findByText(/project state changed/)).toBeTruthy();
    fireEvent.submit(projectForm);
    expect(await screen.findByText(/Project creation was not confirmed/)).toBeTruthy();

    const projectButton = screen.getByRole("button", { name: /Alpha/ });
    fireEvent.click(projectButton);
    expect(await screen.findByText(/permission to load repository grants/)).toBeTruthy();
    fireEvent.click(projectButton);
    expect(await screen.findByText(/Repository grants could not be loaded/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByText(/permission to sign out/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByText(/Sign out was not confirmed/)).toBeTruthy();
  });

  it("loads, creates, selects, registers, and signs out of project workspaces", async () => {
    const fetchMock = vi.mocked(fetch);
    const project = { id: "project-1", name: "Alpha", slug: "alpha" };
    const repository = {
      id: "repository-1",
      target_id: "team/alpha",
      display_name: "Alpha repository",
      default_branch: "main",
    };
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/repositories") && init?.method === "POST") {
        return response(repository, 201) as Response;
      }
      if (url.endsWith("/repositories")) return response([repository]) as Response;
      if (url.endsWith("/projects") && init?.method === "POST") {
        return response(project, 201) as Response;
      }
      if (url.endsWith("/auth/logout")) return response({}, 204) as Response;
      return response([project]) as Response;
    });

    render(<Projects />);
    await screen.findByRole("button", { name: /Alpha/ });
    expect(screen.getByRole("button", { name: /Alpha/ }).getAttribute("aria-pressed")).toBe("false");

    fireEvent.change(screen.getByLabelText("Project name"), { target: { value: "Alpha" } });
    fireEvent.change(screen.getByLabelText("Project slug"), { target: { value: "alpha" } });
    fireEvent.submit(screen.getByRole("heading", { name: "Create project" }).closest("form")!);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));

    fireEvent.click(screen.getByRole("button", { name: /Alpha/ }));
    expect(await screen.findByText("Configured repositories")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Alpha/ }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByLabelText("Default branch")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Repository target ID"), {
      target: { value: "team/alpha" },
    });
    fireEvent.change(screen.getByLabelText("Display name"), {
      target: { value: "Alpha repository" },
    });
    fireEvent.submit(
      screen.getByRole("heading", { name: "Register configured target" }).closest("form")!,
    );
    expect(await screen.findByText("Alpha repository")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/login"));
    expect(fetchMock).toHaveBeenCalledTimes(7);
  });

  it("keeps repository grants aligned with the most recently selected project", async () => {
    const fetchMock = vi.mocked(fetch);
    const projects = [
      { id: "project-alpha", name: "Alpha", slug: "alpha" },
      { id: "project-beta", name: "Beta", slug: "beta" },
    ];
    let resolveAlpha!: (value: Response) => void;
    let resolveBeta!: (value: Response) => void;
    const alphaResponse = new Promise<Response>((resolve) => { resolveAlpha = resolve; });
    const betaResponse = new Promise<Response>((resolve) => { resolveBeta = resolve; });
    fetchMock.mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith("/projects")) return Promise.resolve(response(projects) as Response);
      if (url.endsWith("/project-alpha/repositories")) return alphaResponse;
      if (url.endsWith("/project-beta/repositories")) return betaResponse;
      return Promise.resolve(response({}, 404) as Response);
    });

    render(<Projects />);
    fireEvent.click(await screen.findByRole("button", { name: /Alpha/ }));
    fireEvent.click(screen.getByRole("button", { name: /Beta/ }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));

    resolveBeta(response([{
      id: "repository-beta",
      target_id: "team/beta",
      display_name: "Beta repository",
      default_branch: "main",
    }]) as Response);
    expect(await screen.findByText("Beta repository")).toBeTruthy();

    resolveAlpha(response({}, 401) as Response);
    await waitFor(() => expect(screen.queryByText("Alpha repository")).toBeNull());
    expect(screen.getByRole("heading", { name: "Beta" })).toBeTruthy();
    expect(screen.getByText("Beta repository")).toBeTruthy();
    expect(push).not.toHaveBeenCalled();
  });
});
