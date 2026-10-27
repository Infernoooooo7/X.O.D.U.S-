"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, post, type ApiError, type ConversationDetail, type Message } from "@/lib/api";
import { errorHint, errorTitle } from "@/lib/errors";
import { streamEvents } from "@/lib/sse";
import { useQueryParam, usePoll } from "@/lib/hooks";
import { MESSAGE_STATUS } from "@/lib/status";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Segmented } from "@/components/xodus/segmented";
import { CitedText } from "@/components/xodus/citations";
import { Empty, ErrorNote, PageHeader, Status, Ticket } from "@/components/xodus/parts";
import { TaskTimeline } from "@/components/xodus/task-timeline";

type Mode = "chat" | "ask" | "task";
type Live = { mode: Mode; question: string; text: string; phase: "streaming" | "done" | "error"; error?: { code: string; message: string };
  meta?: Record<string, unknown>; done?: Record<string, unknown> };

const MODES: { id: Mode; label: string; help: string; placeholder: string }[] = [
  { id: "chat", label: "Chat", help: "Talk to the local model. Earlier turns of this conversation are sent with your message.", placeholder: "Ask anything…" },
  { id: "ask", label: "Ask documents", help: "Answer only from your ingested documents, with checked citations. If they do not contain the answer, XODUS says so.", placeholder: "What do my documents say about…" },
  { id: "task", label: "Run task", help: "The model may use capabilities step by step. Anything that changes files waits for your approval.", placeholder: "List the files in my notes folder and summarise them…" },
];

export default function Workspace() {
  const [conv, setConv] = useQueryParam("c");
  const [mode, setMode] = useState<Mode>("chat");
  const [input, setInput] = useState("");
  const [live, setLive] = useState<Live | null>(null);
  const [tasks, setTasks] = useState<{ id: string; goal: string }[]>([]);
  const [error, setError] = useState<ApiError | null>(null);
  const [arm, setArm] = useState("");
  const [chosen, setChosen] = useState<string[] | null>(null);
  const abort = useRef<AbortController | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);

  const detail = usePoll(() => conv ? api<ConversationDetail>(`/api/conversations/${conv}`) : Promise.resolve(null), 0, [conv]);
  const collections = usePoll(() => api<{ id: number; name: string; documents_ready: number }[]>("/api/knowledge/collections"), 0);
  const busy = live?.phase === "streaming";

  useEffect(() => { endRef.current?.scrollIntoView({ block: "nearest" }); }, [detail.data?.items.length, live?.text]);

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
        void detail.refresh();
      } catch (err) {
        setError(err as ApiError);
      }
      return;
    }
    const controller = new AbortController();
    abort.current = controller;
    setLive({ mode, question: text, text: "", phase: "streaming" });
    const url = mode === "chat" ? "/api/chat" : "/api/knowledge/ask";
    const body = mode === "chat" ? { message: text, conversation_id: conversationId }
      : { question: text, conversation_id: conversationId, ...(chosen ? { collections: chosen } : {}) };
    try {
      const { finished } = await streamEvents(url, { method: "POST", json: body }, (ev) => {
        if (ev.event === "meta") setLive((l) => l && { ...l, meta: ev.data });
        else if (ev.event === "token") setLive((l) => l && { ...l, text: l.text + String(ev.data.t ?? "") });
        else if (ev.event === "done") setLive((l) => l && { ...l, phase: "done", done: ev.data, text: String(ev.data.answer ?? l.text) });
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
  const readyCollections = (collections.data ?? []).filter((c) => c.documents_ready > 0);

  return (
    <>
      <PageHeader number="01" title="Workspace" lead={detail.data ? detail.data.title : "A new conversation. It is saved when you send the first message."}>
        {conv && <Button variant="outline" onClick={() => { setConv(null); setLive(null); setTasks([]); }}>New conversation</Button>}
        <Button variant="outline" asChild><Link href="/history/">All conversations</Link></Button>
      </PageHeader>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_24rem]">
        <div className="min-w-0">
          <div aria-label="Conversation" className="space-y-4">
            {detail.error && <ErrorNote error={detail.error} title="This conversation cannot be loaded" />}
            {!items.length && !live && !detail.error && (
              <Empty title="Nothing here yet">Choose how XODUS should answer below, then send a message. Everything stays on this computer.</Empty>
            )}
            {items.map((m) => <Turn key={m.id} m={m} />)}
            {live && <LiveTurn live={live} />}
            <div ref={endRef} />
          </div>

          <form onSubmit={send} className="mt-6 rounded-md border border-border bg-card p-3 sm:p-4">
            <Segmented legend="How XODUS answers" name="mode" value={mode} disabled={busy} onChange={setMode}
              options={MODES.map((m) => ({ value: m.id, label: m.label }))} />
            <p className="mt-2 text-sm text-muted-foreground">{current.help}</p>
            {mode === "ask" && (
              <fieldset className="mt-3">
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
              <div className="mt-3 flex flex-wrap items-center gap-2 text-sm">
                <Label htmlFor="arm">Strategy</Label>
                <select id="arm" value={arm} onChange={(e) => setArm(e.target.value)} className="h-8 rounded-md border border-input bg-background px-2">
                  <option value="">Default (from configuration)</option>
                  <option value="react">Step by step</option>
                  <option value="plan_execute">Plan first, then execute</option>
                </select>
              </div>
            )}
            <Label htmlFor="message" className="sr-only">Message</Label>
            <Textarea id="message" rows={3} className="mt-3" value={input} disabled={busy} placeholder={current.placeholder}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) (e.currentTarget.form as HTMLFormElement).requestSubmit(); }} />
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <Button type="submit" disabled={busy || !input.trim() || (mode === "ask" && chosen?.length === 0)}>
                {mode === "task" ? "Start task" : "Send"}
              </Button>
              {busy && <Button type="button" variant="outline" onClick={() => abort.current?.abort()}>Stop</Button>}
              <span className="text-xs text-muted-foreground">Ctrl+Enter sends</span>
            </div>
            <ErrorNote error={error} title="Not sent" hint={errorHint(error?.code)} />
          </form>
        </div>

        <aside aria-label="Tasks" className="min-w-0 space-y-4">
          {tasks.length === 0 ? (
            <Empty title="No task in this session">Tasks you start appear here with every step, its time since the start, and any approval it needs.</Empty>
          ) : tasks.map((t) => (
            <TaskTimeline key={t.id} taskId={t.id} goal={t.goal} onFinished={() => { setTimeout(() => void detail.refresh(), 400); }} />
          ))}
        </aside>
      </div>
    </>
  );
}

const MODE_LABEL: Record<string, string> = { chat: "chat", ask: "ask documents", task: "task" };

function Turn({ m }: { m: Message }) {
  const badge = MESSAGE_STATUS[m.status];
  if (m.role === "user") {
    return (
      <div className="ml-auto max-w-[min(42rem,92%)] rounded-md bg-secondary px-4 py-3">
        <p className="eyebrow">you, {MODE_LABEL[m.mode]}</p>
        <p className="prose-reply mt-1">{m.content}</p>
      </div>
    );
  }
  const cites = m.meta.citations;
  return (
    <article className="max-w-[min(48rem,100%)] rounded-md border border-border bg-card px-4 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <p className="eyebrow">xodus, {MODE_LABEL[m.mode]}</p>
        {badge && <Status tone={badge.tone}>{badge.label}</Status>}
        {m.meta.insufficient && <Status tone="neutral">Not in your documents</Status>}
        {m.meta.uncited && <Status tone="wait" title="The answer carries no valid citation">No valid citation</Status>}
      </div>
      {m.content ? (
        <div className="mt-1">{m.mode === "ask" ? <CitedText text={m.content} validIds={cites?.valid} /> : <p className="prose-reply">{m.content}</p>}</div>
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
      <Ticket fields={[["model", m.model ?? ""], ["task", m.task_id ?? ""], ["status", m.meta.task_status ?? ""],
        ["ttft", m.meta.timing?.ttft_ms != null ? `${Math.round(m.meta.timing.ttft_ms)} ms` : ""],
        ["total", m.meta.timing?.total_ms != null ? `${Math.round(m.meta.timing.total_ms)} ms` : ""],
        ["tokens", m.meta.usage?.output_tokens ?? ""], ["sources", m.meta.evidence?.length ?? ""], ["code", m.status !== "complete" ? m.meta.code ?? "" : ""]]} />
    </article>
  );
}

function LiveTurn({ live }: { live: Live }) {
  return (
    <>
      <div className="ml-auto max-w-[min(42rem,92%)] rounded-md bg-secondary px-4 py-3">
        <p className="eyebrow">you, {MODE_LABEL[live.mode]}</p>
        <p className="prose-reply mt-1">{live.question}</p>
      </div>
      <article aria-live="polite" className="max-w-[min(48rem,100%)] rounded-md border border-border bg-card px-4 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <p className="eyebrow">xodus, {MODE_LABEL[live.mode]}</p>
          {live.phase === "streaming" && <Status tone="active">Writing, not finished</Status>}
          {live.phase === "done" && <Status tone="good">Finished</Status>}
          {live.phase === "error" && <Status tone={live.error?.code === "cancelled" ? "neutral" : "bad"}>{live.error?.code === "cancelled" ? "Stopped" : "Failed"}</Status>}
        </div>
        {live.mode === "ask" && Array.isArray(live.meta?.evidence) && live.phase === "streaming" && (
          <p className="mt-1 text-xs text-muted-foreground">Found {(live.meta.evidence as unknown[]).length} passages{live.meta.sufficient === false ? ", not enough to answer" : ""}.</p>
        )}
        <p className="prose-reply mt-1">{live.text || (live.phase === "streaming" ? "…" : "")}</p>
        {live.error && <ErrorNote error={live.error} title={errorTitle(live.error.code)} hint={errorHint(live.error.code)} />}
        {live.meta && <Ticket fields={[["request", String(live.meta.request_id ?? "")], ["model", String(live.meta.model ?? "")],
          ["profile", String(live.meta.profile ?? "")], ["context", String(live.meta.num_ctx ?? "")],
          ["earlier turns sent", live.meta.history_turns !== undefined ? String(live.meta.history_turns) : ""]]} />}
      </article>
    </>
  );
}
