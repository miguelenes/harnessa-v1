import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Home from "../app/page";
import Login from "../app/login/page";
import RootLayout from "../app/layout";
import Projects from "../app/projects/page";
import { Button } from "../components/ui/button";

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
    expect(button.className).toContain("border-stone-300");
    expect(button.className).toContain("px-8");
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
    expect(await screen.findByText("Could not load projects")).toBeTruthy();
    second.unmount();

    fetchMock.mockReset();
    fetchMock.mockRejectedValueOnce(new Error("offline fixture"));
    render(<Projects />);
    expect(await screen.findByText("Could not load projects")).toBeTruthy();
  });

  it("reports failed project creation and repository registration", async () => {
    const fetchMock = vi.mocked(fetch);
    const project = { id: "project-1", name: "Alpha", slug: "alpha" };
    fetchMock.mockImplementation(async (_input, init) => {
      if (init?.method === "POST") return response({}, 500) as Response;
      return response([project]) as Response;
    });
    render(<Projects />);
    await screen.findByRole("button", { name: /Alpha/ });

    fireEvent.change(screen.getByPlaceholderText("Project name"), { target: { value: "Alpha" } });
    fireEvent.change(screen.getByPlaceholderText("project-slug"), { target: { value: "alpha" } });
    fireEvent.submit(screen.getByRole("heading", { name: "Create project" }).closest("form")!);
    expect(await screen.findByText("Project creation failed")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /Alpha/ }));
    fireEvent.change(screen.getByPlaceholderText("team/repository"), {
      target: { value: "team/alpha" },
    });
    fireEvent.change(screen.getByPlaceholderText("Display name"), {
      target: { value: "Alpha repository" },
    });
    fireEvent.submit(
      screen.getByRole("heading", { name: "Register configured target" }).closest("form")!,
    );
    expect(
      await screen.findByText(/Repository registration failed\. Confirm target is configured/),
    ).toBeTruthy();
  });

  it("reports rejected credentials and stores the CSRF token after login", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(response({ csrf_token: "fixture-token" }) as Response);
    fetchMock.mockResolvedValueOnce(response({}, 401) as Response);
    const { rerender } = render(<Login />);
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

    fireEvent.change(screen.getByPlaceholderText("Project name"), { target: { value: "Alpha" } });
    fireEvent.change(screen.getByPlaceholderText("project-slug"), { target: { value: "alpha" } });
    fireEvent.submit(screen.getByRole("heading", { name: "Create project" }).closest("form")!);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));

    fireEvent.click(screen.getByRole("button", { name: /Alpha/ }));
    expect(await screen.findByText("Configured repositories")).toBeTruthy();
    fireEvent.change(screen.getByPlaceholderText("team/repository"), {
      target: { value: "team/alpha" },
    });
    fireEvent.change(screen.getByPlaceholderText("Display name"), {
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
});
