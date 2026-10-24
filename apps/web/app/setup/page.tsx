"use client";

import Link from "next/link";
import { useEffect, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { Alert, AlertDescription, Badge, Button, Card, CardContent, CardDescription, CardHeader, CardTitle } from "@harnessa/ui-react";
import type { components } from "@harnessa/contracts";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8010";
type Project = components["schemas"]["ProjectRead"];
type Identity = components["schemas"]["Identity"];
type ModelDoctor = components["schemas"]["ModelDoctorRead"];
type GitHubStatus = components["schemas"]["GitHubConnectionStatusRead"];
type ModelBudget = components["schemas"]["ModelBudgetPolicyRead"];
type Check = { state: "ready" | "blocked" | "unavailable"; detail: string };
type BudgetForm = Pick<ModelBudget, "project_limit_usd" | "task_limit_usd" | "run_limit_usd" | "probe_project_limit_usd" | "probe_task_limit_usd" | "probe_run_limit_usd" | "project_token_limit" | "task_token_limit" | "run_token_limit" | "probe_project_token_limit" | "probe_task_token_limit" | "probe_run_token_limit" | "max_steps" | "max_wall_seconds" | "max_output_tokens" | "max_attempts">;
const EMPTY_BUDGET: BudgetForm = {
  project_limit_usd: "", task_limit_usd: "", run_limit_usd: "",
  probe_project_limit_usd: "", probe_task_limit_usd: "", probe_run_limit_usd: "",
  project_token_limit: 0, task_token_limit: 0, run_token_limit: 0,
  probe_project_token_limit: 0, probe_task_token_limit: 0, probe_run_token_limit: 0,
  max_steps: 0, max_wall_seconds: 0, max_output_tokens: 0, max_attempts: 0,
};

export default function SetupPage() {
  const router = useRouter();
  const [identity, setIdentity] = useState<Identity | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProject, setSelectedProject] = useState("");
  const [repositoryCount, setRepositoryCount] = useState<{ projectId: string; count: number } | null>(null);
  const [budget, setBudget] = useState<ModelBudget | null>(null);
  const [budgetForm, setBudgetForm] = useState<BudgetForm>(EMPTY_BUDGET);
  const [budgetState, setBudgetState] = useState("");
  const [selectedAdapter, setSelectedAdapter] = useState("deployment-default");
  const [adapterDetail, setAdapterDetail] = useState("");
  const [adapterState, setAdapterState] = useState("");
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

  useEffect(() => {
    if (!selectedProject) return;
    let mounted = true;
    void fetch(`${API}/projects/${selectedProject}/execution-adapter`, { credentials: "include", cache: "no-store" })
      .then(async (response) => {
        if (!response.ok) throw new Error("Project execution adapter selection is unavailable.");
        return await response.json() as { adapter: string; detail: string };
      })
      .then((value) => {
        if (mounted) { setSelectedAdapter(value.adapter); setAdapterDetail(value.detail); }
      })
      .catch((cause: unknown) => { if (mounted) setAdapterDetail(cause instanceof Error ? cause.message : "Adapter status unavailable."); });
    return () => { mounted = false; };
  }, [selectedProject]);

  useEffect(() => {
    if (!selectedProject) return;
    let mounted = true;
    void fetch(`${API}/projects/${selectedProject}/models/budget`, { credentials: "include", cache: "no-store" })
      .then(async (response) => {
        if (response.status === 404) return null;
        if (!response.ok) throw new Error("Project budget policy is unavailable.");
        return await response.json() as ModelBudget;
      })
      .then((value) => {
        if (!mounted) return;
        setBudget(value);
        if (value) {
          const { project_limit_usd, task_limit_usd, run_limit_usd, probe_project_limit_usd, probe_task_limit_usd, probe_run_limit_usd, project_token_limit, task_token_limit, run_token_limit, probe_project_token_limit, probe_task_token_limit, probe_run_token_limit, max_steps, max_wall_seconds, max_output_tokens, max_attempts } = value;
          setBudgetForm({ project_limit_usd, task_limit_usd, run_limit_usd, probe_project_limit_usd, probe_task_limit_usd, probe_run_limit_usd, project_token_limit, task_token_limit, run_token_limit, probe_project_token_limit, probe_task_token_limit, probe_run_token_limit, max_steps, max_wall_seconds, max_output_tokens, max_attempts });
        } else setBudgetForm(EMPTY_BUDGET);
      })
      .catch((cause: unknown) => { if (mounted) setError(cause instanceof Error ? cause.message : "Project budget policy is unavailable."); });
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

  async function saveBudget(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedProject || !budgetForm.project_limit_usd || !budgetForm.task_limit_usd || !budgetForm.run_limit_usd || !budgetForm.probe_project_limit_usd || !budgetForm.probe_task_limit_usd || !budgetForm.probe_run_limit_usd) return;
    setBudgetState("Saving separate interactive and probe model policies…");
    setError("");
    try {
      const response = await fetch(`${API}/projects/${selectedProject}/models/budget`, {
        method: "PUT", credentials: "include",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": sessionStorage.getItem("csrf_token") ?? "" },
        body: JSON.stringify({
          ...budgetForm,
          project_limit_usd: budgetForm.project_limit_usd,
          task_limit_usd: budgetForm.task_limit_usd,
          run_limit_usd: budgetForm.run_limit_usd,
          probe_project_limit_usd: budgetForm.probe_project_limit_usd,
          probe_task_limit_usd: budgetForm.probe_task_limit_usd,
          probe_run_limit_usd: budgetForm.probe_run_limit_usd,
        }),
      });
      if (!response.ok) throw new Error(response.status === 403
        ? "Only a project administrator or operator can change budget policy."
        : `Project budget policy could not be saved (${response.status}).`);
      const saved = await response.json() as ModelBudget;
      setBudget(saved);
      setBudgetState("Separate interactive and probe project, task, and run ceilings saved.");
    } catch (cause) {
      setBudgetState("");
      setError(cause instanceof Error ? cause.message : "Project budget policy could not be saved.");
    }
  }

  async function saveAdapter() {
    if (!selectedProject) return;
    setAdapterState("Saving the project runner selection…");
    setError("");
    try {
      const response = await fetch(`${API}/projects/${selectedProject}/execution-adapter`, {
        method: "PUT", credentials: "include",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": sessionStorage.getItem("csrf_token") ?? "" },
        body: JSON.stringify({ adapter: selectedAdapter }),
      });
      if (!response.ok) throw new Error(response.status === 409
        ? "The selected ACP/OpenCode adapter is unavailable in this deployment."
        : `Project adapter selection could not be saved (${response.status}).`);
      const value = await response.json() as { detail: string };
      setAdapterDetail(value.detail);
      setAdapterState("Project execution adapter saved.");
    } catch (cause) {
      setAdapterState("");
      setError(cause instanceof Error ? cause.message : "Project adapter selection could not be saved.");
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
  const routeCheck: Check = modelDoctor?.status === "ready" && modelDoctor.execution_adapter_configured && modelDoctor.execution_model_route_ready
    ? { state: "ready", detail: `${modelDoctor.execution_adapter} · ${modelDoctor.execution_agent_image ?? "agent image unavailable"} · ${modelDoctor.configured_aliases} alias(es), ${modelDoctor.pools.length} pools, LiteLLM process ready. Provider credentials still require an authorized live probe.` }
    : modelDoctor?.status === "ready"
      ? { state: "blocked", detail: `${modelDoctor.execution_adapter_configured ? modelDoctor.execution_adapter : "No execution adapter configured"}; enable the pinned ACP/OpenCode runner and Harnessa model broker in the deployment before paid agent work. ${modelDoctor.configured_aliases} alias(es), ${modelDoctor.pools.length} pools. Provider credentials still require an authorized live probe.` }
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
    <Card className="mt-6"><CardHeader><CardTitle>Project execution adapter</CardTitle><CardDescription>Choose the deployment default, the installed ACP/OpenCode adapter, or disable agent dispatch for this project. The scheduler enforces this selection before creating a run.</CardDescription></CardHeader><CardContent className="space-y-3">
      <label className="grid gap-1 text-sm">Execution adapter<select aria-label="Execution adapter" value={selectedAdapter} onChange={(event) => { setSelectedAdapter(event.target.value); setAdapterState(""); }} disabled={!selectedProject} className="min-h-10 rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3"><option value="deployment-default">Deployment default</option><option value="acp-opencode" disabled={!modelDoctor?.execution_adapter_configured}>ACP / OpenCode{modelDoctor?.execution_adapter_configured ? "" : " (unavailable)"}</option><option value="disabled">Disabled</option></select></label>
      {adapterDetail && <p className="text-sm text-[var(--muted-foreground)]">{adapterDetail}</p>}
      <Button disabled={!selectedProject || (identity?.role !== "administrator" && identity?.role !== "operator")} onClick={() => void saveAdapter()}>Save execution adapter</Button>
      {adapterState && <p role="status" className="text-sm">{adapterState}</p>}
    </CardContent></Card>
    <Card className="mt-6"><CardHeader><CardTitle>Project model budgets</CardTitle><CardDescription>Configure separate finite interactive/primary and probe cost and token ceilings. Both pools remain subject to the project reservation ledger.</CardDescription></CardHeader><CardContent>
      {!selectedProject ? <p role="status" className="text-sm text-[var(--muted-foreground)]">Select or create a project before configuring its budget.</p> : <form className="space-y-4" onSubmit={(event) => void saveBudget(event)}>
        <div className="grid gap-3 sm:grid-cols-3">
          {(["project_limit_usd", "task_limit_usd", "run_limit_usd", "probe_project_limit_usd", "probe_task_limit_usd", "probe_run_limit_usd"] as const).map((key) => <label key={key} className="grid gap-1 text-sm">{key.replaceAll("_", " ")}<input aria-label={key.replaceAll("_", " ")} type="number" min="0.00000001" step="0.00000001" required value={budgetForm[key]} onChange={(event) => setBudgetForm((current) => ({ ...current, [key]: event.target.value }))} className="min-h-10 rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3" /></label>)}
        </div>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {(["project_token_limit", "task_token_limit", "run_token_limit", "probe_project_token_limit", "probe_task_token_limit", "probe_run_token_limit", "max_steps", "max_wall_seconds", "max_output_tokens", "max_attempts"] as const).map((key) => <label key={key} className="grid gap-1 text-sm">{key.replaceAll("_", " ")}<input aria-label={key.replaceAll("_", " ")} type="number" min="1" step="1" required value={budgetForm[key] || ""} onChange={(event) => setBudgetForm((current) => ({ ...current, [key]: Number(event.target.value) }))} className="min-h-10 rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3" /></label>)}
        </div>
        <Button type="submit" disabled={identity?.role !== "administrator" && identity?.role !== "operator"}>{budget ? "Save budget policy" : "Set budget policy"}</Button>
        {budgetState && <p role="status" className="text-sm">{budgetState}</p>}
        {budget && <p className="text-xs text-[var(--muted-foreground)]">Interactive ceilings: project ${budget.project_limit_usd}, task ${budget.task_limit_usd}, run ${budget.run_limit_usd}. Probe ceilings: project ${budget.probe_project_limit_usd}, task ${budget.probe_task_limit_usd}, run ${budget.probe_run_limit_usd}; at most {budget.max_attempts} attempts per request.</p>}
      </form>}
    </CardContent></Card>
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
