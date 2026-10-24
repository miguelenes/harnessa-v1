"use client";

import type { FormEvent } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import type { components } from "@harnessa/contracts";
import {
  Alert,
  AlertDescription,
  Button,
  Card,
  CardContent,
  CardHeader,
  Input,
} from "@harnessa/ui-react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8010";
type Project = components["schemas"]["ProjectRead"];
type ProjectCreate = components["schemas"]["ProjectCreate"];
type Repository = components["schemas"]["RepositoryRead"];
type RepositoryCreate = components["schemas"]["RepositoryCreate"];
type PendingAction = "project" | "repository" | "sign-out" | null;

function errorFor(response: Response, action: string) {
  if (response.status === 401) return "Your session expired. Sign in and try again.";
  if (response.status === 403) return `You do not have permission to ${action}.`;
  if (response.status === 409) return `Could not ${action}; project state changed. Refresh and try again.`;
  return `Could not ${action} (${response.status}).`;
}

export default function Projects() {
  const router = useRouter();
  const [items, setItems] = useState<Project[]>([]);
  const [error, setError] = useState("");
  const [active, setActive] = useState<Project | null>(null);
  const [repos, setRepos] = useState<Repository[]>([]);
  const [loadingRepositories, setLoadingRepositories] = useState(false);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState<PendingAction>(null);
  const repositorySelectionRequest = useRef(0);

  const load = useCallback(async () => {
    setError("");
    setLoading(true);
    try {
      const response = await fetch(`${API}/projects`, { credentials: "include" });
      if (response.status === 401) {
        router.push("/login");
        return;
      }
      if (!response.ok) {
        setError(errorFor(response, "load projects"));
        return;
      }
      setItems((await response.json()) as Project[]);
    } catch {
      setError("Could not reach the Harnessa API. Check the connection and try again.");
    } finally {
      setLoading(false);
    }
  }, [router]);

  useEffect(() => {
    queueMicrotask(() => { void load(); });
  }, [load]);

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const values = new FormData(form);
    setError("");
    setPending("project");
    try {
      const response = await fetch(`${API}/projects`, {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": sessionStorage.getItem("csrf_token") ?? "",
          "Idempotency-Key": crypto.randomUUID(),
        },
        body: JSON.stringify({
          name: String(values.get("name")),
          slug: String(values.get("slug")),
        } satisfies ProjectCreate),
      });
      if (response.status === 401) {
        router.push("/login");
        return;
      }
      if (!response.ok) {
        setError(errorFor(response, "create project"));
        return;
      }
      form.reset();
      await load();
    } catch {
      setError("Could not reach the Harnessa API. Project creation was not confirmed.");
    } finally {
      setPending(null);
    }
  }

  async function select(project: Project) {
    const requestId = ++repositorySelectionRequest.current;
    setActive(project);
    setRepos([]);
    setLoadingRepositories(true);
    setError("");
    try {
      const response = await fetch(`${API}/projects/${project.id}/repositories`, {
        credentials: "include",
      });
      if (requestId !== repositorySelectionRequest.current) return;
      if (response.status === 401) {
        router.push("/login");
        return;
      }
      if (!response.ok) {
        setError(errorFor(response, "load repository grants"));
        return;
      }
      const repositories = (await response.json()) as Repository[];
      if (requestId === repositorySelectionRequest.current) setRepos(repositories);
    } catch {
      if (requestId === repositorySelectionRequest.current) {
        setError("Could not reach the Harnessa API. Repository grants could not be loaded.");
      }
    } finally {
      if (requestId === repositorySelectionRequest.current) setLoadingRepositories(false);
    }
  }

  async function register(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!active) return;
    const form = event.currentTarget;
    const values = new FormData(form);
    setError("");
    setPending("repository");
    try {
      const response = await fetch(`${API}/projects/${active.id}/repositories`, {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": sessionStorage.getItem("csrf_token") ?? "",
          "Idempotency-Key": crypto.randomUUID(),
        },
        body: JSON.stringify({
          target_id: String(values.get("target_id")),
          display_name: String(values.get("display_name")),
          default_branch: String(values.get("default_branch")),
        } satisfies RepositoryCreate),
      });
      if (response.status === 401) {
        router.push("/login");
        return;
      }
      if (!response.ok) {
        setError(response.status === 403
          ? "Repository registration was denied. Confirm the target is configured by the administrator and that you can manage this project."
          : errorFor(response, "register repository"));
        return;
      }
      form.reset();
      await select(active);
    } catch {
      setError("Could not reach the Harnessa API. Repository registration was not confirmed.");
    } finally {
      setPending(null);
    }
  }

  async function signOut() {
    setError("");
    setPending("sign-out");
    try {
      const response = await fetch(`${API}/auth/logout`, {
        method: "POST",
        credentials: "include",
        headers: { "X-CSRF-Token": sessionStorage.getItem("csrf_token") ?? "" },
      });
      if (!response.ok) {
        setError(errorFor(response, "sign out"));
        return;
      }
      router.push("/login");
    } catch {
      setError("Could not reach the Harnessa API. Sign out was not confirmed.");
    } finally {
      setPending(null);
    }
  }

  return (
    <main className="mx-auto min-h-screen max-w-5xl px-4 py-8 sm:px-6 sm:py-12">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[.2em] text-[var(--accent-foreground)]">
            Harnessa
          </p>
          <h1 className="mt-2 text-3xl font-semibold">Projects</h1>
        </div>
        <Button variant="outline" onClick={() => void signOut()} disabled={pending !== null}>
          {pending === "sign-out" ? "Signing out…" : "Sign out"}
        </Button>
      </header>

      {error && (
        <Alert variant="destructive" className="mt-5">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      <div className="mt-8 grid gap-6 md:grid-cols-2">
        <Card>
          <CardHeader>
            <h2 className="text-xl font-semibold">Your projects</h2>
          </CardHeader>
          <CardContent className="space-y-5">
            {loading ? (
              <p role="status" className="text-sm text-muted-foreground">Loading projects…</p>
            ) : items.length === 0 ? (
              <p role="status" className="text-sm text-muted-foreground">No projects yet. Create one to get started.</p>
            ) : (
              <ul className="space-y-2">
                {items.map((project) => (
                  <li key={project.id}>
                    <Button
                      type="button"
                      variant="outline"
                      aria-pressed={active?.id === project.id}
                      onClick={() => void select(project)}
                      disabled={pending !== null}
                      className="h-auto min-h-11 w-full justify-start whitespace-normal px-3 py-2 text-left"
                    >
                      <span className="font-medium">{project.name}</span>
                      <span className="text-sm text-muted-foreground">/{project.slug}</span>
                    </Button>
                  </li>
                ))}
              </ul>
            )}

            <form onSubmit={(event) => void create(event)} className="space-y-3 border-t border-border pt-5">
              <h3 className="font-medium">Create project</h3>
              <label htmlFor="project-name" className="grid gap-1.5 text-sm font-medium">
                Project name
                <Input id="project-name" name="name" required maxLength={120} />
              </label>
              <label htmlFor="project-slug" className="grid gap-1.5 text-sm font-medium">
                Project slug
                <Input
                  id="project-slug"
                  name="slug"
                  required
                  maxLength={80}
                  pattern="[a-z0-9]+(-[a-z0-9]+)*"
                  aria-describedby="project-slug-help"
                />
              </label>
              <p id="project-slug-help" className="text-xs text-muted-foreground">
                Use lowercase letters, numbers, and single hyphens.
              </p>
              <Button type="submit" disabled={pending !== null}>
                {pending === "project" ? "Creating…" : "Create project"}
              </Button>
            </form>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <h2 className="break-words text-xl font-semibold">
              {active ? active.name : "Select a project"}
            </h2>
          </CardHeader>
          <CardContent>
            {active ? (
              <>
                <h3 className="font-medium">Configured repositories</h3>
                {loadingRepositories ? (
                  <p role="status" className="mt-3 text-sm text-muted-foreground">Loading repository grants…</p>
                ) : repos.length > 0 ? (
                  <ul className="mt-3 space-y-2">
                    {repos.map((repository) => (
                      <li key={repository.id} className="rounded-lg border border-border bg-muted p-3">
                        <p className="font-medium">{repository.display_name}</p>
                        <p className="mt-1 break-all text-sm text-muted-foreground">
                          {repository.target_id} · {repository.default_branch}
                        </p>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p role="status" className="mt-3 text-sm text-muted-foreground">
                    No repository grants are configured for this project.
                  </p>
                )}

                <form onSubmit={(event) => void register(event)} className="mt-6 space-y-3 border-t border-border pt-5">
                  <h3 className="font-medium">Register configured target</h3>
                  <p id="repository-target-help" className="text-sm text-muted-foreground">
                    Only target IDs explicitly allowed by the administrator are accepted.
                  </p>
                  <label htmlFor="repository-target" className="grid gap-1.5 text-sm font-medium">
                    Repository target ID
                    <Input
                      id="repository-target"
                      name="target_id"
                      required
                      maxLength={120}
                      aria-describedby="repository-target-help"
                    />
                  </label>
                  <label htmlFor="repository-display-name" className="grid gap-1.5 text-sm font-medium">
                    Display name
                    <Input id="repository-display-name" name="display_name" required maxLength={200} />
                  </label>
                  <label htmlFor="repository-default-branch" className="grid gap-1.5 text-sm font-medium">
                    Default branch
                    <Input id="repository-default-branch" name="default_branch" defaultValue="main" required />
                  </label>
                  <Button type="submit" disabled={pending !== null}>
                    {pending === "repository" ? "Registering…" : "Register repository"}
                  </Button>
                </form>
              </>
            ) : (
              <p role="status" className="text-sm text-muted-foreground">
                Choose a project to view its repository grants.
              </p>
            )}
          </CardContent>
        </Card>
      </div>
    </main>
  );
}
