"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowRight, BookOpen, FileSearch, ListChecks, MessageSquare, Plus, SendHorizontal, Square, type LucideIcon } from "lucide-react";
import { api, post, type ApiError, type ConversationDetail, type Message } from "@/lib/api";
import { COMPOSE_EVENT, isComposeMode, type ComposeMode, type ComposeRequest } from "@/lib/compose";
import { deriveCore, pulseCore } from "@/lib/core-state";
import { errorHint, errorTitle } from "@/lib/errors";
import { streamEvents } from "@/lib/sse";
import { useQueryParam, usePoll } from "@/lib/hooks";
import { MESSAGE_STATUS, TERMINAL_TASK } from "@/lib/status";
import { useSystem } from "@/lib/system-context";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Segmented } from "@/components/xodus/segmented";
import { CitedText } from "@/components/xodus/citations";
import { CoreOrb, CoreStatus } from "@/components/xodus/core-orb";
import { Mark } from "@/components/xodus/mark";
import { Empty, ErrorNote, Status, Ticket } from "@/components/xodus/parts";
import { TaskTimeline } from "@/components/xodus/task-timeline";

type Mode = ComposeMode;
type Live = { mode: Mode; question: string; text: string; chunks: number; phase: "streaming" | "done" | "error";
  sentAfter: number; // how many saved messages the conversation had when this was sent
  error?: { code: string; message: string }; meta?: Record<string, unknown>; done?: Record<string, unknown> };

const MODES: { id: Mode; label: string; help: string; placeholder: string }[] = [
  { id: "chat", label: "Chat", help: "Talk to the local model. Earlier turns of this conversation are sent with your message.", placeholder: "Ask anything…" },
  { id: "ask", label: "Ask documents", help: "Answer only from your ingested documents, with checked citations. If they do not contain the answer, XODUS says so.", placeholder: "What do my documents say about…" },
  { id: "task", label: "Run task", help: "The model may use capabilities step by step. Anything that changes files waits for your approval.", placeholder: "List the files in my notes folder and summarise them…" },
];

/** Ways to start: each one only fills in the message box (and picks the mode); nothing is sent until you send it. */
const STARTERS: { mode: Mode; label: string; text: string }[] = [
  { mode: "ask", label: "Summarise my documents", text: "Summarise what my documents say about " },
  { mode: "task", label: "List my files", text: "List the files in my folder and say what each one seems to be." },
  { mode: "chat", label: "Plan a study session", text: "Make a one-hour study plan for " },
  { mode: "chat", label: "Explain a concept", text: "Explain in simple terms: " },
];

const QUICK: { mode: Mode; icon: LucideIcon; title: string; text: string }[] = [
  { mode: "chat", icon: MessageSquare, title: "Chat", text: "Talk to the local model" },
  { mode: "ask", icon: FileSearch, title: "Ask documents", text: "Answers with checked citations" },
  { mode: "task", icon: ListChecks, title: "Run a task", text: "Changes wait for your approval" },
];

function greetingFor(hour: number): string {
  if (hour >= 5 && hour < 12) return "Good morning";
  if (hour >= 12 && hour < 17) return "Good afternoon";
  if (hour >= 17 && hour < 23) return "Good evening";
  return "Hello";
}

export default function Workspace() {
  const [conv, setConv] = useQueryParam("c");
  const [mode, setMode] = useState<Mode>("chat");
  const [input, setInput] = useState("");
  const [live, setLive] = useState<Live | null>(null);
  const [tasks, setTasks] = useState<{ id: string; goal: string }[]>([]);
  const [taskState, setTaskState] = useState<Record<string, { status: string; steps: number }>>({});
  const [error, setError] = useState<ApiError | null>(null);
  const [arm, setArm] = useState("");
  const [chosen, setChosen] = useState<string[] | null>(null);
  const [greeting, setGreeting] = useState("Hello");
  const abort = useRef<AbortController | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);
  const messageBox = useRef<HTMLTextAreaElement | null>(null);
  const savedCount = useRef(0);
  const system = useSystem();

  const detail = usePoll(() => conv ? api<ConversationDetail>(`/api/conversations/${conv}`) : Promise.resolve(null), 0, [conv]);
  const collections = usePoll(() => api<{ id: number; name: string; documents_ready: number }[]>("/api/knowledge/collections"), 0);
  const busy = live?.phase === "streaming";

  useEffect(() => { endRef.current?.scrollIntoView({ block: "nearest" }); }, [detail.data?.items.length, live?.text]);
  useEffect(() => { setGreeting(greetingFor(new Date().getHours())); }, []);
  useEffect(() => { // a mode asked for from another view (Ctrl+K): /?mode=ask
    const asked = new URLSearchParams(window.location.search).get("mode");
    if (isComposeMode(asked)) { setMode(asked); focusMessage(); }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const focusMessage = () => requestAnimationFrame(() => messageBox.current?.focus());
  const newConversation = useCallback(() => {
    setConv(null); setLive(null); setTasks([]); setTaskState({}); setError(null);
  }, [setConv]);

  useEffect(() => { // requests from the palette while the workspace is open
    const onCompose = (e: Event) => {
      const req = (e as CustomEvent<ComposeRequest>).detail;
      if (req.fresh) newConversation();
      if (req.mode && !busy) setMode(req.mode);
      focusMessage();
    };
    window.addEventListener(COMPOSE_EVENT, onCompose);
    return () => window.removeEventListener(COMPOSE_EVENT, onCompose);
  }, [newConversation, busy]);

  async function ensureConversation(first: string): Promise<number> {
    if (conv) return Number(conv);
    const line = first.split(/\s+/).join(" ").trim();
    const created = await post<{ id: number }>("/api/conversations", { title: line.length > 60 ? `${line.slice(0, 59)}…` : line || "Untitled" });
    setConv(String(created.id));
    return created.id;
  }

  const send = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    const text = input.trim();
    if (!text || busy) return;
    setError(null);
    let conversationId: number;
    try {
      conversationId = await ensureConversation(text);
    } catch (err) {
      setError(err as ApiError);
      return;
    }
    setInput("");
    if (mode === "task") {
      try {
        const res = await post<{ task_id: string }>("/api/tasks", { goal: text, conversation_id: conversationId, ...(arm ? { arm } : {}) });
        setTasks((t) => [{ id: res.task_id, goal: text }, ...t]);
        setTaskState((s) => ({ ...s, [res.task_id]: { status: "queued", steps: 0 } }));
        void detail.refresh();
      } catch (err) {
        setError(err as ApiError);
      }
      return;
    }
    const controller = new AbortController();
    abort.current = controller;
    setLive({ mode, question: text, text: "", chunks: 0, phase: "streaming", sentAfter: savedCount.current });
    const url = mode === "chat" ? "/api/chat" : "/api/knowledge/ask";
    const body = mode === "chat" ? { message: text, conversation_id: conversationId }
      : { question: text, conversation_id: conversationId, ...(chosen ? { collections: chosen } : {}) };
    try {
      const { finished } = await streamEvents(url, { method: "POST", json: body }, (ev) => {
        if (ev.event === "meta") setLive((l) => l && { ...l, meta: ev.data });
        else if (ev.event === "token") {
          pulseCore("token");
          setLive((l) => l && { ...l, text: l.text + String(ev.data.t ?? ""), chunks: l.chunks + 1 });
        } else if (ev.event === "done") setLive((l) => l && { ...l, phase: "done", done: ev.data, text: String(ev.data.answer ?? l.text) });
        else if (ev.event === "error") setLive((l) => l && { ...l, phase: "error", error: { code: String(ev.data.code), message: String(ev.data.message) } });
      }, controller.signal);
      if (!finished) {
        setLive((l) => l && { ...l, phase: "error", error: { code: "stream_incomplete", message: "The response stream ended before the backend confirmed completion." } });
      }
    } catch (err) {
      if (controller.signal.aborted) {
        setLive((l) => l && { ...l, phase: "error", error: { code: "cancelled", message: "Stopped. The partial reply is saved as stopped." } });
      } else {
        setLive((l) => l && { ...l, phase: "error", error: { code: (err as ApiError).code ?? "error", message: (err as ApiError).message ?? String(err) } });
      }
    } finally {
      abort.current = null;
      await detail.refresh();
      // Once the backend has saved the turn (it did if it sent "meta"), the saved copy is the truth: drop the live one.
      // A stop is saved a moment later by the backend, so that live copy stays until the next message.
      setLive((l) => (l && (l.phase === "done" || (l.phase === "error" && l.meta && l.error?.code !== "cancelled")) ? null : l));
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [input, busy, mode, conv, chosen, arm]);

  const current = MODES.find((m) => m.id === mode)!;
  const items = detail.data?.items ?? [];
  savedCount.current = items.length;
  const readyCollections = (collections.data ?? []).filter((c) => c.documents_ready > 0);
  const hero = !conv && !live && tasks.length === 0;
  // The live copy of a question or reply stops being shown the moment the saved conversation contains it, so the two are
  // never on screen together (dropping the live state afterwards is only clean-up). Matched by the saved message id or
  // the request id; by text and mode only among messages saved after this one was sent.
  const liveId = typeof live?.meta?.request_id === "string" ? live.meta.request_id : null;
  const doneId = typeof live?.done?.message_id === "number" ? live.done.message_id : null;
  const fresh = live ? items.slice(live.sentAfter) : [];
  const replySaved = !!live && ((doneId !== null && items.some((m) => m.id === doneId))
    || (liveId !== null && items.some((m) => m.role === "assistant" && m.request_id === liveId))
    || (live.phase === "error" && fresh.some((m) => m.role === "assistant" && m.mode === live.mode)));
  const questionSaved = !!live && ((liveId !== null && items.some((m) => m.role === "user" && m.request_id === liveId))
    || fresh.some((m) => m.role === "user" && m.mode === live.mode && m.content === live.question));
  const shownLive = live && !replySaved ? live : null;

  // The core, from real state only: the health poll, this page's live reply, this session's task timelines, and the
  // last saved reply when it failed (so a failure stays visible until the next message).
  const lastReply = [...items].reverse().find((m) => m.role === "assistant");
  const core = deriveCore({
    backend: system.unreachable ? "down" : system.health ? "up" : "unknown",
    ollama: system.health?.ollama.status ?? null,
    live: shownLive ? { phase: shownLive.phase, chunks: shownLive.chunks, error: shownLive.error?.code }
      : lastReply?.status === "error" ? { phase: "error", chunks: 0, error: lastReply.meta.code ?? "error" } : null,
    tasks: Object.values(taskState).map((t) => t.status),
  });
  const satellites = Object.values(taskState).filter((t) => !TERMINAL_TASK.has(t.status)).reduce((n, t) => n + t.steps, 0);

  function chooseMode(m: Mode) {
    if (!busy) setMode(m);
    focusMessage();
  }

  const composer = (
    <form onSubmit={send} className="glass rounded-2xl p-3 text-left sm:p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Segmented legend="How XODUS answers" name="mode" value={mode} disabled={busy} onChange={setMode}
          options={MODES.map((m) => ({ value: m.id, label: m.label }))} />
        {system.health && (
          <Link href="/models/" title="Change the model profile in the Models view"
            className="flex min-w-0 items-center gap-1.5 rounded-full border border-border px-2.5 py-1 font-mono text-xs text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground">
            <Mark className="size-3.5 shrink-0" />
            <span className="truncate">{system.health.ollama.model} · {system.health.models.num_ctx.toLocaleString()} ctx</span>
          </Link>
        )}
      </div>
      <p className="mt-2 px-1 text-sm text-muted-foreground">{current.help}</p>
      {mode === "ask" && (
        <fieldset className="mt-3 px-1">
          <legend className="text-sm font-medium">Collections</legend>
          {readyCollections.length === 0 ? (
            <p className="text-sm text-muted-foreground">No collection has ready documents. <Link className="underline" href="/knowledge/">Add documents</Link> first.</p>
          ) : (
            <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1">
              {readyCollections.map((c) => {
                const on = chosen === null || chosen.includes(c.name);
                return (
                  <label key={c.id} className="flex items-center gap-1.5 text-sm">
                    <input type="checkbox" checked={on} className="size-4 accent-[var(--primary)]" onChange={() => {
                      const base = chosen ?? readyCollections.map((x) => x.name);
                      setChosen(on ? base.filter((n) => n !== c.name) : [...base, c.name]);
                    }} />
                    {c.name} <span className="font-mono text-xs text-muted-foreground">{c.documents_ready} docs</span>
                  </label>
                );
              })}
            </div>
          )}
        </fieldset>
      )}
      {mode === "task" && (
        <div className="mt-3 flex flex-wrap items-center gap-2 px-1 text-sm">
          <Label htmlFor="arm">Strategy</Label>
          <select id="arm" value={arm} onChange={(e) => setArm(e.target.value)} className="h-8 rounded-lg border border-input bg-background px-2">
            <option value="">Default (from configuration)</option>
            <option value="react">Step by step</option>
            <option value="plan_execute">Plan first, then execute</option>
          </select>
        </div>
      )}
      <Label htmlFor="message" className="sr-only">Message</Label>
      <Textarea ref={messageBox} id="message" rows={hero ? 3 : 2} className="mt-3 resize-none bg-background/50 text-[0.95rem]"
        value={input} disabled={busy} placeholder={current.placeholder}
        onChange={(e) => setInput(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) (e.currentTarget.form as HTMLFormElement).requestSubmit(); }} />
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted-foreground"><span className="kbd">Ctrl</span> + <span className="kbd">Enter</span> sends</span>
        <div className="ml-auto flex items-center gap-2">
          {busy && <Button type="button" variant="outline" onClick={() => abort.current?.abort()}><Square />Stop</Button>}
          <Button type="submit" className="px-4" disabled={busy || !input.trim() || (mode === "ask" && chosen?.length === 0)}>
            <SendHorizontal />{mode === "task" ? "Start task" : "Send"}
          </Button>
        </div>
      </div>
      <ErrorNote error={error} title="Not sent" hint={errorHint(error?.code)} />
    </form>
  );

  if (hero) {
    return (
      <div className="grid gap-8 xl:grid-cols-[minmax(0,1fr)_19rem]">
        <section aria-labelledby="ws-greeting" className="flex min-w-0 flex-col items-center text-center">
          <h1 id="ws-greeting" className="display mt-1 text-[2.5rem] sm:text-[3.5rem]"><span className="sr-only">Workspace. </span>{greeting}.</h1>
          <p className="mt-2 text-lg text-muted-foreground sm:text-xl">How can I help you today?</p>
          <CoreOrb state={core.state} satellites={satellites} className="-mb-6 -mt-2 aspect-square w-[min(84vw,25rem)]" />
          <CoreStatus core={core} className="justify-center" />
          <div className="mt-6 w-full max-w-[44rem]">{composer}</div>
          <div role="group" aria-label="Ways to start (each one only fills in your text; nothing is sent)" className="mt-4 flex max-w-[44rem] flex-wrap justify-center gap-2">
            {STARTERS.map((s) => (
              <button key={s.label} type="button" onClick={() => { setMode(s.mode); setInput(s.text); focusMessage(); }}
                className="rounded-full border border-border bg-card/60 px-3.5 py-1.5 text-sm transition-[border-color,box-shadow] hover:border-primary/50 hover:shadow-[0_0_18px_-8px_rgb(var(--glow)/0.9)]">
                {s.label}
              </button>
            ))}
          </div>
        </section>
        <aside aria-label="Overview" className="space-y-4">
          <ModelPanel />
          <div className="panel p-4">
            <h2 className="text-base font-semibold">Quick actions</h2>
            <ul className="mt-3 space-y-2">
              {QUICK.map((q) => (
                <li key={q.mode}>
                  <button type="button" onClick={() => chooseMode(q.mode)} className={QUICK_ROW}>
                    <QuickInner icon={q.icon} title={q.title} text={q.text} />
                  </button>
                </li>
              ))}
              <li>
                <Link href="/knowledge/" className={QUICK_ROW}>
                  <QuickInner icon={BookOpen} title="Manage knowledge" text="Collections, files, ingestion" />
                </Link>
              </li>
            </ul>
          </div>
        </aside>
      </div>
    );
  }

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_22rem]">
      <div className="min-w-0">
        <div className="sticky top-14 z-20 -mx-4 mb-5 flex items-center gap-3 border-b border-border/60 bg-background/80 px-4 py-2.5 backdrop-blur-xl sm:-mx-8 sm:px-8 xl:mr-0">
          <CoreOrb state={core.state} satellites={satellites} size="compact" className="size-12 shrink-0" />
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-lg font-semibold">{detail.data ? detail.data.title : "New conversation"}</h1>
            <CoreStatus core={core} className="text-xs" />
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Button variant="outline" size="sm" aria-label="New conversation" onClick={newConversation}><Plus /><span className="hidden sm:inline">New conversation</span></Button>
            <Button variant="outline" size="sm" asChild className="hidden md:inline-flex"><Link href="/history/">All conversations</Link></Button>
          </div>
        </div>

        <section aria-label="Conversation" className="space-y-6">
          {detail.error && <ErrorNote error={detail.error} title="This conversation cannot be loaded" />}
          {!items.length && !shownLive && !detail.error && (
            <Empty title="Nothing here yet">Send a message below. Everything stays on this computer.</Empty>
          )}
          {items.map((m) => <Turn key={m.id} m={m} />)}
          {shownLive && <LiveTurn live={shownLive} showQuestion={!questionSaved} />}
          <div ref={endRef} className="scroll-mb-56" />
        </section>
        <div className="sticky bottom-4 z-10 mt-6">{composer}</div>
      </div>

      <aside aria-label="Tasks" className="min-w-0 space-y-4">
        {tasks.length === 0 ? (
          <Empty title="No task in this session">Tasks you start appear here with every step, its time since the start, and any approval it needs.</Empty>
        ) : tasks.map((t) => (
          <TaskTimeline key={t.id} taskId={t.id} goal={t.goal}
            onState={(status, steps) => setTaskState((s) => (s[t.id]?.status === status && s[t.id]?.steps === steps ? s : { ...s, [t.id]: { status, steps } }))}
            onFinished={() => { setTimeout(() => void detail.refresh(), 400); }} />
        ))}
      </aside>
    </div>
  );
}

const QUICK_ROW = "group flex w-full items-center gap-3 rounded-xl border border-border bg-background/40 px-3 py-2.5 text-left transition-[border-color,box-shadow] hover:border-primary/45 hover:shadow-[0_0_22px_-10px_rgb(var(--glow)/0.9)]";

function QuickInner({ icon: Icon, title, text }: { icon: LucideIcon; title: string; text: string }) {
  return (
    <>
      <span className="grid size-9 shrink-0 place-items-center rounded-lg border border-primary/30 bg-accent text-primary">
        <Icon className="size-4" aria-hidden="true" />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block text-sm font-medium">{title}</span>
        <span className="block text-xs text-muted-foreground">{text}</span>
      </span>
      <ArrowRight className="size-4 shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5" aria-hidden="true" />
    </>
  );
}

const OLLAMA_WORDS: Record<string, string> = { ok: "online", unreachable: "not reachable", model_missing: "model missing", remote_model: "model refused (remote)", digest_mismatch: "digest mismatch" };

/** The model in use and where it runs, from the health poll. "On this computer" is shown only when Ollama reports the
 *  configured model as installed and local (remote models are refused by the backend). */
function ModelPanel() {
  const { health, unreachable } = useSystem();
  if (unreachable || !health) {
    return (
      <div className="panel p-4">
        <h2 className="text-base font-semibold">Local model</h2>
        <p className="mt-1 text-sm text-muted-foreground">{unreachable ? "The backend is not answering." : "Checking…"}</p>
      </div>
    );
  }
  const env = health.ollama_environment;
  const ok = health.ollama.status === "ok";
  const summary = !ok ? "Not ready" : env.status === "ok" ? "All checks passing" : env.status === "problem" ? "Ollama settings need attention" : "Ollama settings not verified";
  const where = !ok ? "not available" : env.status === "ok" ? "on this computer, settings checked"
    : env.status === "problem" ? "on this computer; Ollama settings unsafe" : "on this computer; settings not verified";
  return (
    <div className="panel p-4">
      <h2 className="text-base font-semibold">Local model</h2>
      <p className={cn("mt-0.5 flex items-center gap-1.5 text-xs font-medium", ok && env.status === "ok" ? "text-ok" : ok ? "text-warn" : "text-destructive")}>
        <span aria-hidden="true" className="size-1.5 rounded-full bg-current shadow-[0_0_6px_currentColor]" />{summary}
      </p>
      <dl className="mt-3 grid grid-cols-[max-content_minmax(0,1fr)] gap-x-3 gap-y-1.5 text-sm">
        <dt className="text-muted-foreground">Ollama</dt><dd>{OLLAMA_WORDS[health.ollama.status] ?? health.ollama.status}</dd>
        <dt className="text-muted-foreground">Model</dt><dd className="truncate font-mono text-xs leading-5" title={health.ollama.model}>{health.ollama.model}</dd>
        <dt className="text-muted-foreground">Context</dt><dd>{health.models.num_ctx.toLocaleString()} tokens</dd>
        <dt className="text-muted-foreground">Profile</dt><dd>{health.models.switching ? `switching to ${health.models.switching}` : health.models.active}</dd>
        <dt className="text-muted-foreground">Inference</dt><dd>{where}</dd>
      </dl>
    </div>
  );
}

const MODE_LABEL: Record<string, string> = { chat: "chat", ask: "ask documents", task: "task" };

function UserBubble({ mode, text }: { mode: string; text: string }) {
  return (
    <div className="enter ml-auto max-w-[min(40rem,90%)] rounded-2xl rounded-br-md border border-border bg-secondary/80 px-4 py-3">
      <p className="eyebrow">you · {MODE_LABEL[mode]}</p>
      <p className="prose-reply mt-1">{text}</p>
    </div>
  );
}

function ReplyHeader({ mode, children }: { mode: string; children?: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Mark className="size-4" />
      <p className="eyebrow">xodus · {MODE_LABEL[mode]}</p>
      {children}
    </div>
  );
}

function Turn({ m }: { m: Message }) {
  const badge = MESSAGE_STATUS[m.status];
  if (m.role === "user") return <UserBubble mode={m.mode} text={m.content} />;
  const cites = m.meta.citations;
  const more: [string, React.ReactNode][] = [["task", m.task_id ?? ""], ["task status", m.meta.task_status ?? ""],
    ["code", m.status !== "complete" ? m.meta.code ?? "" : ""]];
  return (
    <article className="signal max-w-[min(48rem,100%)] py-1 pl-5" data-live="false">
      <ReplyHeader mode={m.mode}>
        {badge && <Status tone={badge.tone}>{badge.label}</Status>}
        {m.meta.insufficient && <Status tone="neutral">Not in your documents</Status>}
        {m.meta.uncited && <Status tone="wait" title="The answer carries no valid citation">No valid citation</Status>}
      </ReplyHeader>
      {m.content ? (
        <div className="mt-1.5">{m.mode === "ask" ? <CitedText text={m.content} validIds={cites?.valid} /> : <p className="prose-reply">{m.content}</p>}</div>
      ) : (
        <p className="mt-1 text-sm text-muted-foreground">No reply text.</p>
      )}
      {m.status !== "complete" && m.status !== "cancelled" && m.meta.message && (
        <ErrorNote error={{ code: m.meta.code ?? "error", message: m.meta.message }} title={errorTitle(m.meta.code)} hint={errorHint(m.meta.code)} />
      )}
      {m.status === "cancelled" && <p className="mt-2 text-sm text-muted-foreground">Stopped before the model finished; the text above is what had arrived.</p>}
      {cites?.invalid?.length ? (
        <p className="mt-2 text-xs text-muted-foreground">Removed {cites.invalid.length} citation(s) to passages that were not in the evidence.</p>
      ) : null}
      <Ticket fields={[["model", m.model ?? ""],
        ["ttft", m.meta.timing?.ttft_ms != null ? `${Math.round(m.meta.timing.ttft_ms)} ms` : ""],
        ["total", m.meta.timing?.total_ms != null ? `${Math.round(m.meta.timing.total_ms)} ms` : ""],
        ["tokens", m.meta.usage?.output_tokens ?? ""], ["sources", m.meta.evidence?.length ?? ""]]} />
      {more.some(([, v]) => v !== "") && (
        <details className="mt-1">
          <summary className="cursor-pointer text-xs text-muted-foreground hover:text-foreground">More details</summary>
          <Ticket className="mt-1 border-t-0 pt-0" fields={more} />
        </details>
      )}
    </article>
  );
}

function LiveTurn({ live, showQuestion }: { live: Live; showQuestion: boolean }) {
  const streaming = live.phase === "streaming";
  return (
    <>
      {showQuestion && <UserBubble mode={live.mode} text={live.question} />}
      <article aria-live="polite" className="signal max-w-[min(48rem,100%)] py-1 pl-5" data-live={streaming ? "true" : "false"}>
        <ReplyHeader mode={live.mode}>
          {streaming && <Status tone="active">Writing, not finished</Status>}
          {live.phase === "done" && <Status tone="good">Finished</Status>}
          {live.phase === "error" && <Status tone={live.error?.code === "cancelled" ? "neutral" : "bad"}>{live.error?.code === "cancelled" ? "Stopped" : "Failed"}</Status>}
        </ReplyHeader>
        {live.mode === "ask" && Array.isArray(live.meta?.evidence) && streaming && (
          <p className="mt-1 text-xs text-muted-foreground">Found {(live.meta.evidence as unknown[]).length} passages{live.meta.sufficient === false ? ", not enough to answer" : ""}.</p>
        )}
        <p className={cn("prose-reply mt-1.5", streaming && "caret")}>{live.text}</p>
        {live.error && <ErrorNote error={live.error} title={errorTitle(live.error.code)} hint={errorHint(live.error.code)} />}
        {live.meta && <Ticket fields={[["request", String(live.meta.request_id ?? "")], ["model", String(live.meta.model ?? "")],
          ["profile", String(live.meta.profile ?? "")], ["context", String(live.meta.num_ctx ?? "")],
          ["earlier turns sent", live.meta.history_turns !== undefined ? String(live.meta.history_turns) : ""]]} />}
      </article>
    </>
  );
}
