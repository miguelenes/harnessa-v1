"use client";

import { DndContext, type DragEndEvent, type DragStartEvent, KeyboardSensor, PointerSensor, useDraggable, useDroppable, useSensor, useSensors } from "@dnd-kit/core";
import { EventType, type Event as AgUiEvent } from "@ag-ui/core";
import { isRecognizedEvent, type UnrecognizedEvent } from "@ag-ui/client";
import dynamic from "next/dynamic";
import type { components } from "@harnessa/contracts";
import { Alert, AlertDescription, Badge, Button, Card, CardContent, CardDescription, CardHeader, CardTitle } from "@harnessa/ui-react";
import { Activity, BookOpen, Boxes, ClipboardList, FileCode2, GripVertical, LayoutDashboard, LoaderCircle, Moon, Radio, Settings2, Sun } from "lucide-react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { SafeMarkdown } from "./safe-markdown";
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

type Task = components["schemas"]["TaskRead"];
type Project = components["schemas"]["ProjectRead"];
type TaskCapabilities = components["schemas"]["TaskCapabilitiesRead"];
type Interview = components["schemas"]["SpecificationInterviewRead"];
type Question = components["schemas"]["SpecificationQuestionRead"];
type Artifacts = components["schemas"]["SpecificationArtifactRead"];
type ProjectEvent = { cursor: number; event: AgUiEvent };
type Section = "board" | "openspec" | "harness" | "evals" | "routing" | "events";
const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8010";
const NAV: { id: Section; label: string; icon: typeof Activity }[] = [
  { id: "board", label: "Task board", icon: LayoutDashboard },
  { id: "openspec", label: "OpenSpec", icon: BookOpen },
  { id: "harness", label: "Harness Studio", icon: Boxes },
  { id: "evals", label: "Evals", icon: ClipboardList },
  { id: "routing", label: "Routing", icon: Settings2 },
  { id: "events", label: "Events", icon: Radio },
];
const COLUMNS = ["BACKLOG", "SPECIFYING", "READY", "IN_PROGRESS", "VERIFYING", "HUMAN_REVIEW", "LEARNING", "DONE", "CLARIFYING", "HARNESS_PROBE"] as const;
const LABELS: Record<string, string> = { BACKLOG: "Backlog", SPECIFYING: "Specifying", READY: "Ready", IN_PROGRESS: "In progress", VERIFYING: "Verifying", HUMAN_REVIEW: "Human review", LEARNING: "Learning", DONE: "Done", CLARIFYING: "Clarifying", HARNESS_PROBE: "Harness probe", REJECTED: "Rejected", CANCELLED: "Cancelled", RETRY_EXHAUSTED: "Retry exhausted" };
const cardClass = "rounded-xl border border-[var(--border)] bg-[var(--surface)] shadow-sm";
const MonacoEditor = dynamic(() => import("@monaco-editor/react"), { ssr: false, loading: () => <div className="rounded-lg border border-[var(--border)] p-4 text-sm" role="status">Loading code editor…</div> });

function useDnd() { return useSensors(useSensor(PointerSensor), useSensor(KeyboardSensor)); }
function subscribeTheme(callback: () => void) { window.addEventListener("harnessa-theme-change", callback); return () => window.removeEventListener("harnessa-theme-change", callback); }
function readTheme() { return localStorage.getItem("harnessa.theme") === "dark"; }
function eventSummary(event: AgUiEvent): string {
  switch (event.type) {
    case EventType.RUN_STARTED: return "Run started";
    case EventType.RUN_FINISHED: return "Run finished";
    case EventType.RUN_ERROR: return "Run failed";
    case EventType.STEP_STARTED: return "Workflow step started";
    case EventType.STEP_FINISHED: return "Workflow step finished";
    case EventType.TEXT_MESSAGE_CONTENT: return "Assistant update";
    case EventType.TOOL_CALL_START: return "Tool started";
    case EventType.TOOL_CALL_END: return "Tool finished";
    case EventType.STATE_SNAPSHOT: return "Task state updated";
    case EventType.STATE_DELTA: return "Task progress updated";
    case EventType.CUSTOM: return "Workspace update";
    default: return "Workspace update";
  }
}

function TaskCard({ task, onOpen }: { task: Task; onOpen: () => void }) {
  const { attributes, listeners, setActivatorNodeRef, setNodeRef, transform, isDragging } = useDraggable({ id: task.id, data: { task } });
  const dragDescription = [attributes["aria-describedby"], `task-title-${task.id}`].filter(Boolean).join(" ");
  return <div ref={setNodeRef} className={`relative w-full rounded-lg border border-[var(--border)] bg-[var(--surface)] ${isDragging ? "opacity-50" : ""}`} style={transform ? { transform: `translate3d(${transform.x}px, ${transform.y}px, 0)` } : undefined}>
    <button type="button" onClick={onOpen} className="w-full rounded-lg p-3 pr-10 text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--primary)]" aria-label={`${task.title}, ${LABELS[task.stage]}; open task details for authorized transition actions`}>
      <span id={`task-title-${task.id}`} className="block font-medium leading-5">{task.title}</span>
      <span className="mt-2 flex flex-wrap gap-1.5"><Badge variant="outline">{LABELS[task.stage] ?? task.stage}</Badge><Badge variant={task.run_status === "FAILED" ? "destructive" : "secondary"}>{task.run_status}</Badge></span>
      {task.specification_readiness_state && <span className="mt-2 block"><Badge variant={task.specification_readiness_state === "READY" ? "success" : "warning"}>Spec {task.specification_readiness_state} · r{task.specification_revision}</Badge></span>}
      {task.blocked_reason && <span className="mt-2 block text-xs text-[var(--danger)]">Blocked: {task.blocked_reason}</span>}
      <span className="mt-2 block font-mono text-[11px] text-[var(--muted-foreground)]">v{task.version}</span>
    </button>
    <button type="button" ref={setActivatorNodeRef} {...listeners} {...attributes} aria-label={`Drag task card ${task.id.slice(0, 8)}`} aria-describedby={dragDescription} className="absolute right-2 top-2 rounded p-1 text-[var(--muted-foreground)] hover:bg-[var(--muted)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--primary)]"><GripVertical size={16} aria-hidden="true" /></button>
  </div>;
}

function BoardColumn({ stage, tasks, open, disabled }: { stage: string; tasks: Task[]; open: (task: Task) => void; disabled: boolean }) {
  const { setNodeRef, isOver } = useDroppable({ id: stage, disabled });
  return <section ref={setNodeRef} aria-label={`${LABELS[stage]} column${disabled ? ", unavailable as a transition target" : ""}`} className={`flex min-h-56 w-[260px] shrink-0 flex-col gap-2 rounded-xl p-2 ${disabled ? "opacity-75" : isOver ? "bg-[var(--accent)]/10" : "bg-[var(--muted)]/40"}`}>
    <header className="flex items-center justify-between px-1 py-1"><h2 className="text-sm font-semibold">{LABELS[stage]}</h2><Badge variant="secondary">{tasks.length}</Badge></header>
    {tasks.map((task) => <TaskCard key={task.id} task={task} onOpen={() => open(task)} />)}
    {tasks.length === 0 && <p className="px-1 py-3 text-xs text-[var(--muted-foreground)]">No tasks</p>}
  </section>;
}

export function Workspace({ section: rawSection }: { section: string }) {
  const router = useRouter();
  const section = NAV.some((item) => item.id === rawSection) ? rawSection as Section : "board";
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [tasks, setTasks] = useState<Task[]>([]);
  const [active, setActive] = useState<Task | null>(null);
  const [dragTask, setDragTask] = useState<Task | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(true);
  const [live, setLive] = useState(false);
  const [projectEvents, setProjectEvents] = useState<ProjectEvent[]>([]);
  const dark = useSyncExternalStore(subscribeTheme, readTheme, () => false);
  const sensors = useDnd();
  const activeTaskId = useRef<string | undefined>(undefined);
  useEffect(() => { activeTaskId.current = active?.id; }, [active?.id]);
  const closeInspector = useCallback(() => setActive(null), []);
  const project = projects.find((item) => item.id === projectId);

  const loadProjects = useCallback(async () => {
    const response = await fetch(`${API}/projects`, { credentials: "include" });
    if (response.status === 401) { router.push("/login"); return; }
    if (!response.ok) throw new Error(response.status === 403 ? "You do not have access to projects." : "Project list is unavailable.");
    const rows: Project[] = await response.json(); setProjects(rows);
    const remembered = typeof window === "undefined" ? "" : localStorage.getItem("harnessa.project") ?? "";
    setProjectId(rows.some((row) => row.id === remembered) ? remembered : rows[0]?.id ?? "");
  }, [router]);

  const loadTasks = useCallback(async (selected = projectId) => {
    if (!selected) { setTasks([]); setLoading(false); return; }
    setLoading(true);
    const response = await fetch(`${API}/projects/${selected}/tasks?limit=500`, { credentials: "include", cache: "no-store" });
    if (response.status === 401) { router.push("/login"); return; }
    if (!response.ok) throw new Error(response.status === 403 ? "Access to this project was denied." : "Task board could not be loaded.");
    setTasks(await response.json()); setLoading(false);
  }, [projectId, router]);

  useEffect(() => { queueMicrotask(() => { loadProjects().catch((cause: unknown) => setError(cause instanceof Error ? cause.message : "Project load failed")); }); }, [loadProjects]);
  useEffect(() => { if (!projectId) return; localStorage.setItem("harnessa.project", projectId); queueMicrotask(() => { loadTasks(projectId).catch((cause: unknown) => { setError(cause instanceof Error ? cause.message : "Task load failed"); setLoading(false); }); }); }, [projectId, loadTasks]);
  useEffect(() => {
    if (!projectId) return;
    let stopped = false;
    let controller: AbortController | undefined;
    const key = `harnessa.cursor.${projectId}`;
    async function connect() {
      let cursor = Number(sessionStorage.getItem(key) ?? "0");
      setProjectEvents([]);
      while (!stopped) {
        controller = new AbortController();
        try {
          const response = await fetch(`${API}/projects/${projectId}/events/stream?after=${cursor}`, { credentials: "include", headers: { Accept: "text/event-stream" }, signal: controller.signal, cache: "no-store" });
          if (response.status === 410) {
            const detail = await response.json().catch(() => ({}));
            await loadTasks(projectId);
            cursor = Number(detail?.detail?.oldest_available_cursor ?? 0);
            sessionStorage.setItem(key, String(cursor));
            setProjectEvents([]);
            continue;
          }
          if (response.status === 403) { setError("Event access was revoked. Reopen the workspace after your project grants are updated."); break; }
          if (!response.ok || !response.body) throw new Error(`Event stream unavailable (${response.status}).`);
          setLive(true);
          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          let buffer = "";
          while (!stopped) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let boundary: number;
            while ((boundary = buffer.indexOf("\n\n")) >= 0) {
              const frame = buffer.slice(0, boundary);
              buffer = buffer.slice(boundary + 2);
              const id = /^id: (\d+)$/m.exec(frame)?.[1];
              const data = /^data: (.+)$/m.exec(frame)?.[1];
              if (!id) continue;
              const sequence = Number(id);
              if (sequence <= cursor) continue;
              // Advance even when the encoded event is malformed or from a future extension.
              cursor = sequence;
              sessionStorage.setItem(key, String(cursor));
              if (!data) continue;
              try {
                const candidate = JSON.parse(data) as UnrecognizedEvent;
                if (!candidate || typeof candidate !== "object" || typeof candidate.type !== "string") continue;
                if (!isRecognizedEvent(candidate)) continue;
                const event = candidate as AgUiEvent;
                setProjectEvents((current) => [...current.slice(-199), { cursor: sequence, event }]);
                if (event.type === EventType.CUSTOM && event.name === "harnessa.access_revoked") {
                  setError("Project access changed. Event access was revoked.");
                  stopped = true;
                  controller?.abort();
                  break;
                }
                await loadTasks(projectId);
                const selectedId = activeTaskId.current;
                if (selectedId) {
                  const latest = await fetch(`${API}/tasks/${selectedId}`, { credentials: "include", cache: "no-store" });
                  if (latest.ok) { const updated: Task = await latest.json(); setActive((current) => current?.id === updated.id ? updated : current); }
                }
              } catch {
                // The durable cursor is still safe to advance past an invalid frame.
              }
            }
          }
          setLive(false);
          if (!stopped) await new Promise((resolve) => setTimeout(resolve, 700));
        } catch {
          setLive(false);
          if (!stopped) await new Promise((resolve) => setTimeout(resolve, 1200));
        }
      }
    }
    void connect();
    return () => { stopped = true; controller?.abort(); setLive(false); };
  }, [loadTasks, projectId]);
  useEffect(() => { document.documentElement.dataset.theme = dark ? "dark" : "light"; }, [dark]);
  function toggleTheme() { const next = !dark; localStorage.setItem("harnessa.theme", next ? "dark" : "light"); document.documentElement.dataset.theme = next ? "dark" : "light"; window.dispatchEvent(new Event("harnessa-theme-change")); }

  async function move(task: Task, target: string): Promise<Task | null> {
    if (target === task.stage) return task;
    if (!(task.allowed_transitions ?? []).includes(target as Task["stage"])) {
      setError("This stage is not an authorized transition for your current project role.");
      return null;
    }
    setError(""); setNotice("");
    const optimistic = { ...task, stage: target as Task["stage"] };
    setTasks((current) => current.map((item) => item.id === task.id ? optimistic : item));
    const returnStage = target === "CLARIFYING" || target === "HARNESS_PROBE" ? task.stage : task.return_stage;
    let response: Response;
    try {
      response = await fetch(`${API}/tasks/${task.id}/transition`, { method: "POST", credentials: "include", headers: { "Content-Type": "application/json", "X-CSRF-Token": sessionStorage.getItem("csrf_token") ?? "", "If-Match": String(task.version), "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify({ target_stage: target, reason: "Moved from the task board", return_stage: returnStage }) });
    } catch {
      try { await loadTasks(); }
      catch { setTasks((current) => current.map((item) => item.id === task.id ? task : item)); }
      setError("The transition response was lost. The board was refreshed; verify the current task state before trying again.");
      return null;
    }
    if (response.ok) { const updated: Task = await response.json(); setTasks((current) => current.map((item) => item.id === task.id ? updated : item)); setActive((current) => current?.id === task.id ? updated : current); return updated; }
    const detail = response.status === 403 ? "This transition is not authorized for your project role." : response.status === 409 || response.status === 412 ? "The task changed in another session. The board has been refreshed." : `Transition was rejected (${response.status}).`;
    if (response.status === 409 || response.status === 412) {
      try { await loadTasks(); }
      catch { setTasks((current) => current.map((item) => item.id === task.id ? task : item)); }
    } else setTasks((current) => current.map((item) => item.id === task.id ? task : item));
    setError(detail);
    return null;
  }

  function onDragStart(event: DragStartEvent) {
    setDragTask(event.active.data.current?.task as Task | null ?? null);
  }

  function onDragEnd(event: DragEndEvent) {
    const task = event.active.data.current?.task as Task | undefined;
    if (task && typeof event.over?.id === "string") void move(task, event.over.id);
    setDragTask(null);
  }

  async function logout() { await fetch(`${API}/auth/logout`, { method: "POST", credentials: "include", headers: { "X-CSRF-Token": sessionStorage.getItem("csrf_token") ?? "" } }); router.push("/login"); }
  async function refreshTask(taskId: string): Promise<Task | null> {
    await loadTasks();
    const response = await fetch(`${API}/tasks/${taskId}`, { credentials: "include", cache: "no-store" });
    if (!response.ok) return null;
    const updated: Task = await response.json();
    setActive((current) => current?.id === taskId ? updated : current);
    return updated;
  }

  return <main className="min-h-screen bg-[var(--background)] text-[var(--foreground)]">
    <div className="mx-auto flex min-h-screen max-w-[1600px] flex-col lg:flex-row">
      <aside className="flex w-full shrink-0 flex-col border-b border-[var(--border)] bg-[var(--surface)] p-4 lg:min-h-screen lg:w-60 lg:border-b-0 lg:border-r lg:p-5">
        <Link href="/workspace/board" className="flex items-center gap-2 font-semibold tracking-tight"><span className="grid size-8 place-items-center rounded-lg bg-[var(--primary)] text-white" aria-hidden="true"><svg viewBox="0 0 24 24" className="size-5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M6 4H4v16h2M18 4h2v16h-2M8 12l2.5 2.5L16 9" /></svg></span>Harnessa</Link>
        <label className="mt-6 text-xs font-medium text-[var(--muted-foreground)]" htmlFor="project-switch">Project</label>
        <select id="project-switch" className="mt-2 min-h-10 rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--primary)]" value={projectId} onChange={(event) => { setProjectId(event.target.value); setActive(null); }}>
          {projects.length === 0 && <option value="">No authorized projects</option>}{projects.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
        <nav aria-label="Workspace" className="mt-5 flex gap-1 overflow-x-auto lg:flex-col">{NAV.map(({ id, label, icon: Icon }) => <Link key={id} href={`/workspace/${id}`} aria-current={section === id ? "page" : undefined} className={`flex min-h-10 shrink-0 items-center gap-3 rounded-lg px-3 text-sm ${section === id ? "bg-[var(--primary)]/10 font-semibold text-[var(--primary)]" : "text-[var(--muted-foreground)] hover:bg-[var(--muted)]"}`}><Icon size={17} aria-hidden="true" />{label}</Link>)}</nav>
        <div className="mt-auto hidden border-t border-[var(--border)] pt-4 lg:block"><p className="truncate text-xs text-[var(--muted-foreground)]">{project?.name ?? "Select a project"}</p><Button variant="ghost" className="mt-2 w-full justify-start" onClick={logout}>Sign out</Button></div>
      </aside>
      <section className="min-w-0 flex-1 p-4 sm:p-6 lg:p-8">
        <header className="mb-6 flex flex-wrap items-center justify-between gap-3"><div><p className="text-xs font-medium uppercase tracking-[.12em] text-[var(--muted-foreground)]">{project?.name ?? "Workspace"}</p><h1 className="mt-1 text-2xl font-semibold">{NAV.find((item) => item.id === section)?.label}</h1></div><div className="flex items-center gap-2"><Badge variant={live ? "default" : "outline"}>{live ? "Live events connected" : "Event stream idle"}</Badge><Button variant="outline" size="icon" aria-label={dark ? "Use light theme" : "Use dark theme"} onClick={toggleTheme}>{dark ? <Sun aria-hidden="true" /> : <Moon aria-hidden="true" />}</Button><Button variant="outline" onClick={() => loadTasks().catch((cause: unknown) => setError(cause instanceof Error ? cause.message : "Refresh failed"))}>Refresh</Button><Button className="lg:hidden" variant="ghost" onClick={logout}>Sign out</Button></div></header>
        {error && <Alert variant="destructive" className="mb-4"><AlertDescription>{error}</AlertDescription></Alert>}{notice && <Alert className="mb-4"><AlertDescription>{notice}</AlertDescription></Alert>}
        {!projectId ? <Card><CardHeader><CardTitle>No project selected</CardTitle><CardDescription>Sign in with a project membership or create a project from setup.</CardDescription></CardHeader></Card> : section === "board" ? <>
          <div className="mb-4 flex flex-wrap items-center justify-between gap-2"><p className="text-sm text-[var(--muted-foreground)]">Task stage, run status, and specification readiness are shown separately. Move cards with drag and drop, or open details and use the authorized transition buttons.</p><Button onClick={async () => { const title = window.prompt("Task title"); if (!title?.trim()) return; const response = await fetch(`${API}/projects/${projectId}/tasks`, { method: "POST", credentials: "include", headers: { "Content-Type": "application/json", "X-CSRF-Token": sessionStorage.getItem("csrf_token") ?? "", "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify({ title: title.trim(), description: "", priority: 0 }) }); if (!response.ok) { setError(response.status === 403 ? "Task creation is not authorized." : "Task creation failed."); return; } await loadTasks(); }}>Create task</Button></div>
          {loading ? <div role="status" className="flex items-center gap-2 py-12 text-sm"><LoaderCircle className="animate-spin" size={18} />Loading tasks…</div> : tasks.length === 0 ? <Card><CardHeader><CardTitle>No tasks yet</CardTitle><CardDescription>Create a task to start a governed workflow.</CardDescription></CardHeader></Card> : <DndContext sensors={sensors} onDragStart={onDragStart} onDragEnd={onDragEnd} onDragCancel={() => setDragTask(null)}><div className="flex gap-3 overflow-x-auto pb-4" aria-label="Ten-column task board">{COLUMNS.map((stage) => <BoardColumn key={stage} stage={stage} tasks={tasks.filter((task) => task.stage === stage)} open={setActive} disabled={dragTask ? !(dragTask.allowed_transitions ?? []).includes(stage) : false} />)}</div></DndContext>}
          {!loading && tasks.some((task) => ["REJECTED", "CANCELLED", "RETRY_EXHAUSTED"].includes(task.stage)) && <section aria-label="Terminal tasks" className="mt-5"><h2 className="mb-2 text-sm font-semibold">Terminal tasks</h2><ul className="space-y-2">{tasks.filter((task) => ["REJECTED", "CANCELLED", "RETRY_EXHAUSTED"].includes(task.stage)).map((task) => <li key={task.id}><button className="flex w-full flex-wrap items-center justify-between gap-2 rounded-lg border border-[var(--border)] bg-[var(--surface)] p-3 text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--primary)]" onClick={() => setActive(task)}><span className="font-medium">{task.title}</span><span className="flex flex-wrap gap-1.5"><Badge variant="outline">{LABELS[task.stage]}</Badge><Badge variant={task.run_status === "FAILED" ? "destructive" : "secondary"}>{task.run_status}</Badge>{task.specification_readiness_state && <Badge variant="outline">Spec {task.specification_readiness_state} · r{task.specification_revision}</Badge>}</span></button></li>)}</ul></section>}
          {active && <TaskInspector task={active} onClose={closeInspector} onMove={(target) => move(active, target)} onRefresh={() => refreshTask(active.id)} onError={setError} />}
        </> : section === "events" ? <EventsView events={projectEvents} connected={live} /> : section === "openspec" ? <OpenSpecView projectId={projectId} /> : section === "harness" ? <HarnessStudioView projectId={projectId} tasks={tasks} /> : section === "evals" ? <EvaluationsView tasks={tasks} /> : <RoutingView projectId={projectId} />}
      </section>
    </div>
  </main>;
}

function TaskInspector({ task, onClose, onMove, onRefresh, onError }: { task: Task; onClose: () => void; onMove: (stage: string) => Promise<Task | null>; onRefresh: () => Promise<Task | null>; onError: (message: string) => void }) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const [runs, setRuns] = useState<components["schemas"]["RunRead"][]>([]);
  const [capabilities, setCapabilities] = useState<TaskCapabilities | null>(null);
  const [interview, setInterview] = useState<Interview | null>(null);
  const [questions, setQuestions] = useState<Question[]>([]);
  const [artifacts, setArtifacts] = useState<Artifacts | null>(null);
  const [artifactLoading, setArtifactLoading] = useState(false);
  const [artifactError, setArtifactError] = useState("");
  const [selectedArtifact, setSelectedArtifact] = useState<"proposal_md" | "spec_md" | "design_md" | "tasks_md">("proposal_md");
  const [answerDraft, setAnswerDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const [actionNotice, setActionNotice] = useState("");
  const [tab, setTab] = useState("Activity");
  const [loading, setLoading] = useState(true);
  const commandKeys = useRef<Record<string, string>>({});
  const answerKey = useRef<string | null>(null);
  const csrf = () => sessionStorage.getItem("csrf_token") ?? "";
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const dialog = dialogRef.current;
    const focusable = () => Array.from(dialog?.querySelectorAll<HTMLElement>("button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])") ?? []);
    focusable()[0]?.focus();
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") { event.preventDefault(); onClose(); return; }
      if (event.key !== "Tab") return;
      const items = focusable();
      if (!items.length) return;
      if (event.shiftKey && document.activeElement === items[0]) { event.preventDefault(); items.at(-1)?.focus(); }
      else if (!event.shiftKey && document.activeElement === items.at(-1)) { event.preventDefault(); items[0]?.focus(); }
    }
    dialog?.addEventListener("keydown", onKeyDown);
    return () => { dialog?.removeEventListener("keydown", onKeyDown); previous?.focus(); };
  }, [onClose]);
  const command = async (name: "cancel" | "pause" | "resume" | "retry") => {
    setBusy(true); setActionError(""); setActionNotice("");
    const idempotencyKey = commandKeys.current[name] ??= crypto.randomUUID();
    let response: Response;
    try {
      response = await fetch(`${API}/tasks/${task.id}/commands`, { method: "POST", credentials: "include", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf(), "If-Match": String(task.version), "Idempotency-Key": idempotencyKey }, body: JSON.stringify({ command: name, reason: `User requested ${name} from the task inspector` }) });
    } catch {
      setActionError(`The ${name} request lost its connection. Retry to safely check the same command.`);
      setBusy(false);
      return;
    }
    if (response.ok) {
      delete commandKeys.current[name];
      setActionNotice(`Task ${name} command accepted.`);
      await onRefresh();
      const [runResponse, capsResponse] = await Promise.all([
        fetch(`${API}/tasks/${task.id}/runs`, { credentials: "include", cache: "no-store" }),
        fetch(`${API}/tasks/${task.id}/capabilities`, { credentials: "include", cache: "no-store" }),
      ]);
      if (runResponse.ok) setRuns(await runResponse.json());
      if (capsResponse.ok) setCapabilities(await capsResponse.json());
      setBusy(false);
      return;
    }
    if (response.status === 409 || response.status === 412) {
      delete commandKeys.current[name];
      await onRefresh();
      const policy = await fetch(`${API}/tasks/${task.id}/capabilities`, { credentials: "include", cache: "no-store" });
      if (policy.ok) setCapabilities(await policy.json());
    }
    setActionError(response.status === 403 ? `Your project role cannot ${name} this run.` : response.status === 409 || response.status === 412 ? `The task changed before ${name}; its current version and allowed actions were refreshed.` : `Could not ${name} this task (${response.status}).`); setBusy(false);
  };
  const loadArtifactBundle = async () => {
    if (!interview || artifacts || artifactLoading) return;
    setArtifactLoading(true); setArtifactError("");
    try {
      const response = await fetch(`${API}/projects/${task.project_id}/specification-interviews/${interview.id}/artifacts`, { credentials: "include" });
      if (response.status === 403) throw new Error("Specification artifacts are not authorized for this account.");
      if (response.status === 404) throw new Error("No artifact revision is available yet.");
      if (!response.ok) throw new Error("Specification artifacts could not be loaded.");
      setArtifacts(await response.json());
    } catch (cause) {
      setArtifactError(cause instanceof Error ? cause.message : "Specification artifacts could not be loaded.");
    } finally {
      setArtifactLoading(false);
    }
  };
  useEffect(() => { let mounted = true; void (async () => {
    const [runResponse, capsResponse, interviewResponse] = await Promise.all([
      fetch(`${API}/tasks/${task.id}/runs`, { credentials: "include" }),
      fetch(`${API}/tasks/${task.id}/capabilities`, { credentials: "include" }),
      fetch(`${API}/tasks/${task.id}/specification-interview`, { credentials: "include" }),
    ]);
    if (runResponse.status === 403 || capsResponse.status === 403) throw new Error("Task inspection is not authorized for this account.");
    if (interviewResponse.status === 403) setActionError("Specification interview access is denied for this account.");
    if (!runResponse.ok || !capsResponse.ok) throw new Error("Task activity or action policy is unavailable.");
    const [runRows, caps] = await Promise.all([runResponse.json(), capsResponse.json()]);
    if (!mounted) return;
    setRuns(runRows); setCapabilities(caps);
    if (interviewResponse.ok) {
      const data: Interview = await interviewResponse.json(); if (!mounted) return; setInterview(data);
      const questionResponse = await fetch(`${API}/projects/${task.project_id}/specification-interviews/${data.id}/questions`, { credentials: "include" });
      if (questionResponse.ok && mounted) setQuestions(await questionResponse.json());
    }
  })().catch((cause: unknown) => { if (mounted) onError(cause instanceof Error ? cause.message : "Task inspector unavailable"); }).finally(() => { if (mounted) setLoading(false); }); return () => { mounted = false; }; }, [task.id, task.project_id, onError]);
  async function submitAnswer(question: Question) {
    if (!interview || !answerDraft.trim()) return;
    setBusy(true); setActionError("");
    const idempotencyKey = answerKey.current ??= crypto.randomUUID();
    let response: Response;
    try {
      response = await fetch(`${API}/projects/${task.project_id}/specification-interviews/${interview.id}/questions/${question.id}/answers`, { method: "POST", credentials: "include", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf(), "Idempotency-Key": idempotencyKey }, body: JSON.stringify({ expected_revision: interview.revision, answer: answerDraft.trim(), changes_scope: false }) });
    } catch {
      setActionError("The answer request lost its connection. Retry to safely check the same answer."); setBusy(false); return;
    }
    if (!response.ok) { if (response.status === 409) answerKey.current = null; setActionError(response.status === 403 ? "Answer submission is not authorized." : response.status === 409 ? "This question is based on a stale revision. Refresh the interview." : `Answer could not be submitted (${response.status}).`); setBusy(false); return; }
    const result = await response.json(); answerKey.current = null; setAnswerDraft(""); setInterview(result.interview); setActionNotice("Answer recorded with an idempotency key.");
    const questionResponse = await fetch(`${API}/projects/${task.project_id}/specification-interviews/${interview.id}/questions`, { credentials: "include" }); if (questionResponse.ok) setQuestions(await questionResponse.json()); await onRefresh(); setBusy(false);
  }
  async function saveArtifacts() {
    if (!interview || !artifacts || !capabilities?.can_edit_artifacts) return;
    setBusy(true); setActionError("");
    const body = { expected_revision: interview.revision, proposal_md: artifacts.proposal_md, spec_md: artifacts.spec_md, design_md: artifacts.design_md, tasks_md: artifacts.tasks_md };
    let response: Response;
    try {
      response = await fetch(`${API}/projects/${task.project_id}/specification-interviews/${interview.id}/artifacts`, { method: "PUT", credentials: "include", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf(), "If-Match": String(interview.revision) }, body: JSON.stringify(body) });
    } catch {
      setActionError("The artifact save lost its connection. Retry to safely check the same revision."); setBusy(false); return;
    }
    if (!response.ok) { setActionError(response.status === 403 ? "Artifact editing is not authorized." : response.status === 409 || response.status === 412 ? "The specification revision changed. Reload before saving." : `Artifact update failed (${response.status}).`); setBusy(false); return; }
    setInterview(await response.json()); setArtifacts(null); setActionNotice("Artifact revision saved. Readiness must be evaluated again."); setBusy(false); await onRefresh();
  }
  async function moveAndRefresh(stage: string) {
    const updated = await onMove(stage);
    if (!updated) return;
    const [runsResponse, capabilitiesResponse] = await Promise.all([
      fetch(`${API}/tasks/${task.id}/runs`, { credentials: "include", cache: "no-store" }),
      fetch(`${API}/tasks/${task.id}/capabilities`, { credentials: "include", cache: "no-store" }),
    ]);
    if (runsResponse.ok) setRuns(await runsResponse.json());
    if (capabilitiesResponse.ok) setCapabilities(await capabilitiesResponse.json());
  }
  return <div ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="inspector-title" className="fixed inset-0 z-20 flex justify-end bg-black/30"><section className="flex h-full w-full max-w-2xl flex-col overflow-auto border-l border-[var(--border)] bg-[var(--background)] p-5 shadow-xl"><header className="flex items-start justify-between gap-3"><div><h2 id="inspector-title" className="text-xl font-semibold">{task.title}</h2><p className="mt-1 font-mono text-xs text-[var(--muted-foreground)]">Task {task.id} · version {task.version}</p></div><Button variant="outline" onClick={onClose}>Close</Button></header><div className="mt-4 flex flex-wrap gap-2"><Badge>{LABELS[task.stage]}</Badge><Badge variant={task.run_status === "FAILED" ? "destructive" : "secondary"}>{task.run_status}</Badge>{interview && <Badge variant={interview.readiness_state === "READY" ? "success" : "warning"}>Spec {interview.readiness_state} · r{interview.revision}</Badge>}</div>
    {actionError && <Alert variant="destructive" className="mt-4"><AlertDescription>{actionError}</AlertDescription></Alert>}{actionNotice && <Alert className="mt-4"><AlertDescription>{actionNotice}</AlertDescription></Alert>}
    <div className="mt-5 flex gap-1 overflow-x-auto" role="tablist" aria-label="Task details">{["Activity", "Artifacts", "Interview", "Clarification", "Probe / review"].map((name) => <button key={name} role="tab" aria-selected={tab === name} className="rounded-md px-3 py-2 text-sm aria-selected:bg-[var(--primary)]/10 aria-selected:text-[var(--primary)] focus-visible:outline focus-visible:outline-2" onClick={() => { setTab(name); if (name === "Artifacts") void loadArtifactBundle(); }}>{name}</button>)}</div>
    <div className="mt-5 flex-1">{loading ? <p role="status">Loading task activity and action policy…</p> : tab === "Activity" ? <>
      {runs.length ? <ol className="space-y-3">{runs.map((run) => <li key={run.id} className={cardClass + " p-3"}><p className="font-medium">Run {run.attempt} · {run.status}</p><p className="mt-1 text-xs text-[var(--muted-foreground)]">{run.started_at ? new Date(run.started_at).toLocaleString() : "Waiting to start"}</p>{run.error_code && <p className="mt-2 text-sm text-[var(--danger)]">This run failed. Review authorized diagnostics for details.</p>}</li>)}</ol> : <p className="text-sm text-[var(--muted-foreground)]">No runs recorded for this task.</p>}
      {(capabilities?.allowed_commands ?? []).length ? <div className="mt-4 flex flex-wrap gap-2">{(capabilities?.allowed_commands ?? []).map((name) => <Button key={name} variant={name === "cancel" ? "destructive" : "outline"} disabled={busy} onClick={() => void command(name)}>{name[0]?.toUpperCase()}{name.slice(1)} run</Button>)}</div> : null}
    </> : tab === "Artifacts" ? !interview ? <p className="text-sm text-[var(--muted-foreground)]">No specification interview is attached to this task.</p> : artifactLoading ? <p role="status" className="text-sm">Loading authorized artifacts…</p> : artifactError ? <div role="alert" className="space-y-2 text-sm"><p>{artifactError}</p>{!artifactError.includes("No artifact revision") && <Button variant="outline" onClick={() => void loadArtifactBundle()}>Retry artifact load</Button>}</div> : !artifacts ? <p className="text-sm text-[var(--muted-foreground)]">No artifact revision is available yet.</p> : <div className="space-y-3"><label className="block text-sm font-medium" htmlFor="artifact-file">OpenSpec artifact</label><select id="artifact-file" value={selectedArtifact} onChange={(event) => setSelectedArtifact(event.target.value as typeof selectedArtifact)} className="min-h-10 rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3">{([["proposal_md", "Proposal"], ["spec_md", "Specification"], ["design_md", "Design"], ["tasks_md", "Tasks"]] as const).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select><div className="overflow-hidden rounded-lg border border-[var(--border)]"><MonacoEditor height="340px" language="markdown" theme={typeof document !== "undefined" && document.documentElement.dataset.theme === "dark" ? "vs-dark" : "light"} value={artifacts[selectedArtifact]} onChange={(value) => setArtifacts({ ...artifacts, [selectedArtifact]: value ?? "" })} options={{ minimap: { enabled: false }, wordWrap: "on", readOnly: !capabilities?.can_edit_artifacts, ariaLabel: "OpenSpec artifact editor" }} /></div><details><summary className="cursor-pointer text-sm">Safe Markdown preview</summary><div className="prose prose-sm mt-3 max-w-none"><SafeMarkdown>{artifacts[selectedArtifact]}</SafeMarkdown></div></details><details><summary className="cursor-pointer text-sm">Review the generated diff</summary><MonacoEditor height="220px" language="diff" value={artifacts.diff} options={{ minimap: { enabled: false }, readOnly: true, wordWrap: "on", ariaLabel: "OpenSpec change diff" }} /></details>{capabilities?.can_edit_artifacts && <Button disabled={busy} onClick={() => void saveArtifacts()}>Save artifact revision</Button>}</div> : tab === "Interview" ? !interview ? <p className="text-sm text-[var(--muted-foreground)]">No interview is attached to this task.</p> : <div className="space-y-3"><p className="text-sm text-[var(--muted-foreground)]">Revision {interview.revision} · readiness {interview.readiness_state}</p>{questions.length === 0 ? <Card><CardHeader><CardTitle>No open questions</CardTitle><CardDescription>Questions recorded by the authorized interview workflow appear here.</CardDescription></CardHeader></Card> : questions.map((question) => <Card key={question.id}><CardHeader><CardTitle>{question.question}</CardTitle><CardDescription>{question.status} · {question.classification} · affects {(question.affected_artifacts ?? []).join(", ") || "specification"}</CardDescription></CardHeader>{question.status === "open" && capabilities?.can_answer_questions && <CardContent><label className="mb-2 block text-sm" htmlFor={`answer-${question.id}`}>Answer</label><textarea id={`answer-${question.id}`} value={answerDraft} onChange={(event) => { answerKey.current = null; setAnswerDraft(event.target.value); }} className="min-h-24 w-full rounded-lg border border-[var(--border)] bg-[var(--surface)] p-3" /><Button className="mt-2" disabled={busy || !answerDraft.trim()} onClick={() => void submitAnswer(question)}>Submit answer</Button></CardContent>}</Card>)}</div> : <Card><CardHeader><CardTitle>{tab}</CardTitle><CardDescription>{tab === "Clarification" ? "Clarification questions and answers are shown in the interview tab." : "Probe and review evidence appears here when a supported backend workflow records it."}</CardDescription></CardHeader></Card>}
      {capabilities && (capabilities.allowed_transitions ?? []).length > 0 && <div className="mt-6 border-t border-[var(--border)] pt-4"><p className="mb-2 text-sm font-medium">Authorized stage transitions</p><div className="flex flex-wrap gap-2">{(capabilities.allowed_transitions ?? []).map((stage) => <Button key={stage} size="sm" variant="outline" aria-label={`Move to ${LABELS[stage]}`} onClick={() => void moveAndRefresh(stage)}>{LABELS[stage]}</Button>)}</div><p className="mt-2 text-xs text-[var(--muted-foreground)]">Keyboard users can tab to these actions and activate them with Enter or Space.</p></div>}</div></section></div>;
}

function OpenSpecView({ projectId }: { projectId: string }) {
  const [entries, setEntries] = useState<components["schemas"]["SpecificationExplorerEntry"][]>([]);
  const [error, setError] = useState("");
  useEffect(() => { let mounted = true; void (async () => { const reposResponse = await fetch(`${API}/projects/${projectId}/repositories`, { credentials: "include" }); if (!reposResponse.ok) throw new Error(reposResponse.status === 403 ? "Repository access denied." : "Connected repositories are unavailable."); const repos = await reposResponse.json() as components["schemas"]["RepositoryRead"][]; const results = await Promise.all(repos.map(async (repo) => { const response = await fetch(`${API}/projects/${projectId}/repositories/${repo.id}/specification-explorer`, { credentials: "include" }); if (!response.ok) throw new Error(response.status === 403 ? "OpenSpec access denied." : "OpenSpec explorer is unavailable."); return await response.json() as components["schemas"]["SpecificationExplorerRead"]; })); if (mounted) setEntries(results.flatMap((result) => result.entries)); })().catch((cause: unknown) => { if (mounted) setError(cause instanceof Error ? cause.message : "OpenSpec data unavailable"); }); return () => { mounted = false; }; }, [projectId]);
  return <div className="space-y-4"><p className="text-sm text-[var(--muted-foreground)]">Repository specifications are read from the authorized OpenSpec explorer.</p>{error && <Alert variant="destructive"><AlertDescription>{error}</AlertDescription></Alert>}{entries.length === 0 ? <Card><CardHeader><CardTitle>No specification changes</CardTitle><CardDescription>Connected repositories with OpenSpec changes will appear here.</CardDescription></CardHeader></Card> : entries.map((entry) => <Card key={`${entry.revision}:${entry.path}`}><CardHeader><CardTitle className="flex items-center gap-2"><FileCode2 size={17} />{entry.path}</CardTitle><CardDescription>{entry.section} · {entry.revision}{entry.archived ? " · archived" : ""}</CardDescription></CardHeader></Card>)}</div>;
}

function useProjectRuns(projectId: string, tasks: Task[]) {
  const [runs, setRuns] = useState<Record<string, components["schemas"]["RunRead"][]>>({});
  const [error, setError] = useState("");
  const boundedTasks = useMemo(() => tasks.slice(0, 50), [tasks]);
  useEffect(() => {
    let mounted = true;
    if (boundedTasks.length === 0) {
      void Promise.resolve().then(() => { if (mounted) { setRuns({}); setError(""); } });
      return () => { mounted = false; };
    }
    void Promise.all(boundedTasks.map(async (task) => {
      const response = await fetch(`${API}/tasks/${task.id}/runs`, { credentials: "include", cache: "no-store" });
      if (!response.ok) throw new Error(response.status === 403 ? "Run evidence is not authorized for this project." : "Run evidence is unavailable.");
      return [task.id, await response.json() as components["schemas"]["RunRead"][]] as const;
    })).then((rows) => { if (mounted) { setRuns(Object.fromEntries(rows)); setError(""); } }).catch((cause: unknown) => { if (mounted) setError(cause instanceof Error ? cause.message : "Run evidence is unavailable."); });
    return () => { mounted = false; };
  }, [boundedTasks, projectId]);
  return { runs, error };
}

function HarnessStudioView({ projectId, tasks }: { projectId: string; tasks: Task[] }) {
  const visibleTasks = useMemo(() => tasks.slice(0, 50), [tasks]);
  const { runs, error } = useProjectRuns(projectId, visibleTasks);
  return <div className="space-y-4"><Card><CardHeader><CardTitle>Harness Studio</CardTitle><CardDescription>Authorized task and run activity for the most recent 50 tasks. Opening this view does not start a worker.</CardDescription></CardHeader></Card>{error && <Alert variant="destructive"><AlertDescription>{error}</AlertDescription></Alert>}{visibleTasks.length === 0 ? <Card><CardHeader><CardTitle>No task runs</CardTitle><CardDescription>Run activity appears when the scheduler claims an executable task.</CardDescription></CardHeader></Card> : visibleTasks.map((task) => <Card key={task.id}><CardHeader><CardTitle>{task.title}</CardTitle><CardDescription>{LABELS[task.stage]} · task v{task.version}</CardDescription></CardHeader><CardContent>{(runs[task.id] ?? []).length === 0 ? <p className="text-sm text-[var(--muted-foreground)]">No run has been claimed by the scheduler.</p> : <ul className="space-y-2">{(runs[task.id] ?? []).map((run) => <li key={run.id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-[var(--border)] p-3"><span>Attempt {run.attempt}</span><Badge variant={run.status === "FAILED" ? "destructive" : "secondary"}>{run.status}</Badge><span className="text-xs text-[var(--muted-foreground)]">{run.started_at ? new Date(run.started_at).toLocaleString() : "Not started"}</span></li>)}</ul>}</CardContent></Card>)}</div>;
}

function EvaluationsView({ tasks }: { tasks: Task[] }) {
  const visibleTasks = useMemo(() => tasks.slice(0, 50), [tasks]);
  return <div className="space-y-4"><Card><CardHeader><CardTitle>Evaluation evidence</CardTitle><CardDescription>Specification readiness is shown separately from verifier outcomes. Task or run status is never treated as an evaluation result.</CardDescription></CardHeader></Card>{visibleTasks.length === 0 ? <Card><CardHeader><CardTitle>No task evidence</CardTitle><CardDescription>Create or select a task to see its specification readiness state.</CardDescription></CardHeader></Card> : visibleTasks.map((task) => <Card key={task.id}><CardHeader><CardTitle>{task.title}</CardTitle><CardDescription>{LABELS[task.stage]} · run {task.run_status}</CardDescription></CardHeader><CardContent>{task.specification_readiness_state ? <div className="flex flex-wrap items-center gap-2"><Badge variant={task.specification_readiness_state === "READY" ? "success" : "warning"}>Specification {task.specification_readiness_state}</Badge><span className="text-sm text-[var(--muted-foreground)]">Revision {task.specification_revision}</span></div> : <p className="text-sm text-[var(--muted-foreground)]">No specification readiness record is attached.</p>}<p className="mt-2 text-xs text-[var(--muted-foreground)]">Verifier evaluation records require a project-scoped evaluation API that is not present in the current backend.</p></CardContent></Card>)}</div>;
}

function RoutingView({ projectId }: { projectId: string }) {
  const [usage, setUsage] = useState<components["schemas"]["ModelUsageRead"] | null>(null);
  const [budget, setBudget] = useState<components["schemas"]["ModelBudgetPolicyRead"] | null>(null);
  const [requests, setRequests] = useState<components["schemas"]["ModelRequestLedgerRead"][]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  useEffect(() => { let mounted = true; void (async () => {
    const [usageResponse, requestResponse, budgetResponse] = await Promise.all([
      fetch(`${API}/projects/${projectId}/models/usage`, { credentials: "include", cache: "no-store" }),
      fetch(`${API}/projects/${projectId}/models/requests?limit=50`, { credentials: "include", cache: "no-store" }),
      fetch(`${API}/projects/${projectId}/models/budget`, { credentials: "include", cache: "no-store" }),
    ]);
    if (usageResponse.status === 403 || requestResponse.status === 403 || budgetResponse.status === 403) throw new Error("Model routing data is not authorized for this project.");
    if (!usageResponse.ok || !requestResponse.ok || (budgetResponse.status !== 404 && !budgetResponse.ok)) throw new Error("Model routing and usage data are unavailable.");
    const [usageRow, requestRows, budgetRow] = await Promise.all([usageResponse.json(), requestResponse.json(), budgetResponse.status === 404 ? Promise.resolve(null) : budgetResponse.json()]);
    if (mounted) { setUsage(usageRow); setRequests(requestRows); setBudget(budgetRow); }
  })().catch((cause: unknown) => { if (mounted) setError(cause instanceof Error ? cause.message : "Model routing data is unavailable."); }).finally(() => { if (mounted) setLoading(false); }); return () => { mounted = false; }; }, [projectId]);
  return <div className="space-y-4"><Card><CardHeader><CardTitle>Model routing</CardTitle><CardDescription>Project-scoped usage, budgets, and redacted request ledger from authorized backend APIs. Provider credentials and endpoint secrets are never displayed.</CardDescription></CardHeader>{loading && <CardContent><p role="status">Loading routing status…</p></CardContent>}</Card>{error && <Alert variant="destructive"><AlertDescription>{error}</AlertDescription></Alert>}{usage && <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">{[["Billed spend", `$${usage.billed_usd}`], ["Estimated spend", `$${usage.estimated_usd}`], ["Input tokens", usage.input_tokens.toLocaleString()], ["Output tokens", usage.output_tokens.toLocaleString()], ["Unknown usage", usage.unknown_usage_requests], ["Pending reservations", usage.pending_reservations]].map(([label, value]) => <Card key={label}><CardContent className="pt-5"><p className="text-xs text-[var(--muted-foreground)]">{label}</p><p className="mt-1 text-xl font-semibold">{value}</p></CardContent></Card>)}</div>}{budget ? <Card><CardHeader><CardTitle>Project budget policy</CardTitle><CardDescription>Per-project, task, and run limits · maximum {budget.max_attempts} attempts</CardDescription></CardHeader><CardContent><p>Project limit ${budget.project_limit_usd} · task limit ${budget.task_limit_usd} · run limit ${budget.run_limit_usd}</p></CardContent></Card> : !loading && <Card><CardHeader><CardTitle>No project budget configured</CardTitle><CardDescription>An administrator must configure a budget before paid routing actions can run.</CardDescription></CardHeader></Card>}{requests.length > 0 && <Card><CardHeader><CardTitle>Recent model requests</CardTitle><CardDescription>Ledger entries are read-only in this workspace.</CardDescription></CardHeader><CardContent><ul className="space-y-2">{requests.map((request) => <li key={request.id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-[var(--border)] p-3"><span>{request.model_alias}</span><span className="text-xs text-[var(--muted-foreground)]">{request.pool_name} · {request.event_type} · attempt {request.attempt}</span></li>)}</ul></CardContent></Card>}</div>;
}

function EventsView({ events, connected }: { events: ProjectEvent[]; connected: boolean }) {
  return <div className="space-y-4"><Card><CardHeader><CardTitle>Durable project events</CardTitle><CardDescription>{connected ? "Connected. Replaying durable events and following new updates." : "Connecting or reconnecting to durable event replay…"}</CardDescription></CardHeader><CardContent><p className="text-xs text-[var(--muted-foreground)]">Reconnect resumes persisted events; it does not start or cancel a run.</p></CardContent></Card>{events.length === 0 ? <Card><CardContent className="pt-5 text-sm text-[var(--muted-foreground)]">No retained events have arrived for this project.</CardContent></Card> : events.map(({ cursor: sequence, event }) => <Card key={sequence}><CardContent className="flex items-start gap-3 pt-4"><Activity size={17} className="mt-0.5 shrink-0 text-[var(--primary)]" aria-hidden="true" /><div className="min-w-0"><p className="text-sm font-medium">{eventSummary(event)}</p></div></CardContent></Card>)}</div>;
}
