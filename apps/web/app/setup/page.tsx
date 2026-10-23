"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Alert, AlertDescription, Badge, Button, Card, CardContent, CardDescription, CardHeader, CardTitle } from "@harnessa/ui-react";
import type { components } from "@harnessa/contracts";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8010";
type Project = components["schemas"]["ProjectRead"];
type Identity = components["schemas"]["Identity"];
type ModelDoctor = components["schemas"]["ModelDoctorRead"];
type GitHubStatus = components["schemas"]["GitHubConnectionStatusRead"];
type Check = { state: "ready" | "blocked" | "unavailable"; detail: string };

export default function SetupPage() {
  const router = useRouter();
  const [identity, setIdentity] = useState<Identity | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProject, setSelectedProject] = useState("");
  const [repositoryCount, setRepositoryCount] = useState<{ projectId: string; count: number } | null>(null);
  const [modelDoctor, setModelDoctor] = useState<ModelDoctor | null>(null);
  const [github, setGithub] = useState<GitHubStatus | null>(null);
  const [runtime, setRuntime] = useState<Check>({ state: "unavailable", detail: "Checking runtime readiness…" });
  const [alias, setAlias] = useState("harnessa-deepseek-flash");
  const [authorizePaid, setAuthorizePaid] = useState(false);
  const [probeState, setProbeState] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let mounted = true;
    const read = async (path: string) => {
      const response = await fetch(`${API}${path}`, { credentials: "include", cache: "no-store" });
      if (response.status === 401) { router.push("/login"); throw new Error("Sign in to complete setup."); }
      if (!response.ok) throw new Error(`Setup check unavailable (${response.status}).`);
      return response.json();
    };
    void Promise.allSettled([
      read("/auth/me"), read("/projects"), read("/ops/models/doctor"), read("/github/status"),
      fetch(`${API}/health/ready`, { cache: "no-store" }).then((response) => {
        if (!response.ok) throw new Error(`Runtime readiness returned ${response.status}.`);
        return { state: "ready", detail: "API dependencies report ready." } satisfies Check;
      }),
    ]).then((results) => {
      if (!mounted) return;
      const [who, projectRows, model, githubStatus, health] = results;
      if (who.status === "fulfilled") setIdentity(who.value as Identity);
      if (projectRows.status === "fulfilled") {
        const rows = projectRows.value as Project[];
        setProjects(rows);
        setSelectedProject(rows[0]?.id ?? "");
      }
      if (model.status === "fulfilled") setModelDoctor(model.value as ModelDoctor);
      if (githubStatus.status === "fulfilled") setGithub(githubStatus.value as GitHubStatus);
      if (health.status === "fulfilled") setRuntime(health.value as Check);
      else setRuntime({ state: "unavailable", detail: "API dependency readiness could not be verified." });
      const failures = results.slice(0, 4).filter((item) => item.status === "rejected");
      if (failures.length) setError("Some administrator-only setup checks are unavailable. Confirm your role and refresh the page.");
    });
    return () => { mounted = false; };
  }, [router]);

  useEffect(() => {
    if (!selectedProject) return;
    let mounted = true;
    void fetch(`${API}/projects/${selectedProject}/repositories`, { credentials: "include", cache: "no-store" })
      .then(async (response) => {
        if (!response.ok) throw new Error("Repository grants are unavailable.");
        return await response.json() as unknown[];
      })
      .then((rows) => { if (mounted) setRepositoryCount({ projectId: selectedProject, count: rows.length }); })
      .catch(() => { if (mounted) setRepositoryCount({ projectId: selectedProject, count: -1 }); });
    return () => { mounted = false; };
  }, [selectedProject]);

  async function verifyRoute() {
    if (!selectedProject || !authorizePaid || !alias.trim()) return;
    setProbeState("Sending the bounded request through the project budget policy…");
    setError("");
    try {
      const response = await fetch(`${API}/projects/${selectedProject}/models/live-check`, {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": sessionStorage.getItem("csrf_token") ?? "", "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ alias: alias.trim(), authorize_paid_request: true }),
      });
      if (!response.ok) throw new Error(response.status === 403 ? "An organization administrator must authorize this route check." : `Route check could not be completed (${response.status}).`);
      const result = await response.json() as components["schemas"]["ModelLiveCheckRead"];
      setProbeState(result.outcome === "passed"
        ? `Passed · ${result.deployment} · billed ${result.billed_usd ?? "unknown"} · usage ${result.usage_known ? "known" : "unknown"}`
        : `Failed · ${result.error_code ?? "provider or route unavailable"}`);
    } catch (cause) {
      setProbeState("");
      setError(cause instanceof Error ? cause.message : "Route check failed.");
    }
  }

  const identityCheck: Check = identity
    ? { state: "ready", detail: `${identity.email} · ${identity.role}` }
    : { state: "unavailable", detail: "Administrator identity could not be verified." };
  const currentRepositoryCount = repositoryCount?.projectId === selectedProject ? repositoryCount.count : null;
  const grantCheck: Check = currentRepositoryCount === null || currentRepositoryCount < 0
    ? { state: "unavailable", detail: "Choose a project with an accessible repository grant." }
    : currentRepositoryCount > 0
      ? { state: "ready", detail: `${currentRepositoryCount} repository grant${currentRepositoryCount === 1 ? "" : "s"} configured for the selected project.` }
      : { state: "blocked", detail: "No repository grant is configured for the selected project." };
  const routeCheck: Check = modelDoctor?.status === "ready"
    ? { state: "ready", detail: `${modelDoctor.configured_aliases} alias(es), ${modelDoctor.pools.length} pool(s), LiteLLM process ready. Provider credentials still require an authorized live probe.` }
    : modelDoctor
      ? { state: "blocked", detail: (modelDoctor.findings ?? []).join(" ") || "Model routing setup is incomplete." }
      : { state: "unavailable", detail: "Administrator model-routing status is unavailable." };
  const githubCheck: Check = github
    ? github.configured_apps.some((app) => app.connection_status === "ready") && github.installations.some((item) => item.active)
      ? { state: "ready", detail: `${github.installations.filter((item) => item.active).length} active installation(s); review repository opt-in status in GitHub.` }
      : { state: "blocked", detail: "GitHub App and active installation are not fully configured. Repository mutations remain unavailable." }
    : { state: "unavailable", detail: "GitHub setup status is unavailable." };
  const checks = [
    ["Administrator identity", identityCheck, "/projects"],
    ["Repository grants", grantCheck, "/projects"],
    ["Agent and route capability", routeCheck, "/workspace/routing"],
    ["GitHub installation", githubCheck, "/workspace/github"],
    ["Runtime health", runtime, "/setup"],
  ] as const;

  return <main className="mx-auto min-h-screen max-w-5xl px-4 py-8 sm:px-6 sm:py-12">
    <header className="flex flex-wrap items-center justify-between gap-3">
      <div><p className="text-xs font-semibold uppercase tracking-[.2em] text-[var(--accent)]">Harnessa setup</p><h1 className="mt-2 text-3xl font-semibold">Configuration and readiness</h1></div>
      <Link href="/projects" className="rounded-lg border border-[var(--border)] px-4 py-2 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--primary)]">Projects and repository grants</Link>
    </header>
    {error && <Alert variant="destructive" className="mt-5"><AlertDescription>{error}</AlertDescription></Alert>}
    <section aria-label="Setup checklist" className="mt-8 grid gap-4 md:grid-cols-2">
      {checks.map(([name, check, href]) => <Card key={name}>
        <CardHeader><div className="flex flex-wrap items-center justify-between gap-2"><CardTitle>{name}</CardTitle><Badge variant={check.state === "ready" ? "success" : "warning"}>{check.state}</Badge></div><CardDescription>{check.detail}</CardDescription></CardHeader>
        <CardContent>{href !== "/setup" && <Link className="text-sm text-[var(--primary)] underline underline-offset-4" href={href}>Open {name.toLowerCase()} settings</Link>}</CardContent>
      </Card>)}
    </section>
    <Card className="mt-6"><CardHeader><CardTitle>Verify a provider route</CardTitle><CardDescription>This sends one minimal, paid request through Harnessa’s existing project, task, and run budget controls. It does not start an agent task. Provider credentials remain in LiteLLM.</CardDescription></CardHeader>
      <CardContent className="space-y-4">
        <label className="grid gap-1 text-sm">Project<select value={selectedProject} onChange={(event) => setSelectedProject(event.target.value)} className="min-h-10 rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3" disabled={!projects.length}><option value="">Select a project</option>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select></label>
        <label className="grid gap-1 text-sm">Model alias<input value={alias} onChange={(event) => setAlias(event.target.value)} className="min-h-10 rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3" autoComplete="off" /></label>
        <label className="flex items-start gap-3 text-sm"><input type="checkbox" checked={authorizePaid} onChange={(event) => setAuthorizePaid(event.target.checked)} className="mt-1 size-4 accent-[var(--primary)]" /><span>I authorize one paid route check, bounded by the selected project’s existing budget policy.</span></label>
        <Button disabled={!selectedProject || !authorizePaid || !alias.trim() || probeState.startsWith("Sending") || identity?.role !== "administrator"} onClick={() => void verifyRoute()}>{probeState.startsWith("Sending") ? "Checking route…" : "Run one-token route check"}</Button>
        {probeState && <p role="status" className="text-sm">{probeState}</p>}
        {identity && identity.role !== "administrator" && <p className="text-sm text-[var(--muted-foreground)]">Only an organization administrator can authorize a paid route check.</p>}
      </CardContent>
    </Card>
  </main>;
}
