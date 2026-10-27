"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { post, type Approval, type TaskEvent } from "@/lib/api";
import { streamEvents } from "@/lib/sse";
import { countdown, shortHash, tplus } from "@/lib/format";
import { useAction, useNow } from "@/lib/hooks";
import { taskStatus, type Tone } from "@/lib/status";
import { Button } from "@/components/ui/button";
import { ErrorNote, JsonView, Status, Tag } from "./parts";

type Props = { taskId: string; goal?: string; onFinished?: (status: string, answer: string | null) => void };

/** Live view of one task: every backend event in order, against its T+ offset from the start of the task. */
export function TaskTimeline({ taskId, goal, onFinished }: Props) {
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const [answer, setAnswer] = useState("");
  const [finished, setFinished] = useState<TaskEvent | null>(null);
  const [lost, setLost] = useState(false);
  const [streamError, setStreamError] = useState<{ code: string; message: string } | null>(null);
  const lastSeq = useRef(0);
  const cancel = useAction();
  const finishedRef = useRef(onFinished);
  finishedRef.current = onFinished;

  const follow = useCallback((signal: AbortSignal) => {
    setLost(false);
    setStreamError(null);
    streamEvents(`/api/tasks/${taskId}/events?after=${lastSeq.current}`, { method: "GET" }, (e) => {
      const ev: TaskEvent = { ...(e.data as TaskEvent), type: e.event };
      if (typeof ev.seq === "number") lastSeq.current = Math.max(lastSeq.current, ev.seq);
      if (ev.type === "token") {
        setAnswer((a) => a + String(ev.t ?? ""));
        return;
      }
      if (ev.type === "answer") setAnswer(String(ev.text ?? ""));
      if (ev.type === "task_finished") {
        setFinished(ev);
        finishedRef.current?.(String(ev.status), (ev.answer as string | null) ?? null);
      }
      setEvents((list) => [...list, ev]);
    }, signal, (e) => e.event === "task_finished").then(({ finished: done }) => {
      if (!done && !signal.aborted) setLost(true);
    }, (err) => {
      if (!signal.aborted) {
        setLost(true);
        setStreamError({ code: err.code ?? "stream_error", message: err.message ?? String(err) });
      }
    });
  }, [taskId]);

  useEffect(() => {
    const controller = new AbortController();
    lastSeq.current = 0;
    setEvents([]);
    setAnswer("");
    setFinished(null);
    follow(controller.signal);
    return () => controller.abort();
  }, [follow]);

  const waiting = !finished && events.some((e) => e.type === "approval_required") &&
    !events.some((e, i) => i > events.findLastIndex((x) => x.type === "approval_required") &&
      ["approval_resolved", "step_blocked", "step_cancelled"].includes(e.type));
  const state = finished ? String(finished.status) : waiting ? "awaiting_approval" : events.length ? "running" : "queued";
  const status = taskStatus(state);

  return (
    <section aria-label="Task timeline" className="rounded-md border border-border bg-card p-4 sm:p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="eyebrow">task {taskId}</p>
          {goal && <h3 className="mt-0.5 text-base font-semibold [overflow-wrap:anywhere]">{goal}</h3>}
        </div>
        <div className="flex items-center gap-2">
          <span role="status" aria-live="polite"><Status tone={status.tone} title={status.explain}>{status.label}</Status></span>
          {!finished && (
            <Button size="sm" variant="outline" disabled={cancel.busy}
              onClick={() => cancel.run(() => post(`/api/tasks/${taskId}/cancel`))}>
              {cancel.busy ? "Cancelling…" : "Cancel task"}
            </Button>
          )}
        </div>
      </div>
      <ErrorNote error={cancel.error} title="Could not cancel" />

      <ol className="timeline mt-4">
        {events.filter((e) => e.type !== "answer" && e.type !== "task_finished").map((e) => (
          <EventRow key={e.seq} e={e} finished={Boolean(finished)} />
        ))}
        {(answer || finished) && (
          <li data-tone={finished ? toneOf(String(finished.status)) : "active"}>
            <span className="t">{finished ? tplus(finished.t_ms) : "…"}</span>
            <div className="body">
              <p className="text-sm font-medium">{finished ? status.label : "Answer (still being written)"}</p>
              {answer && <p className="prose-reply mt-1 text-sm">{answer}</p>}
              {finished?.message ? <p className="mt-1 text-sm text-muted-foreground">{String(finished.message)}</p> : null}
            </div>
          </li>
        )}
      </ol>

      {lost && !finished && (
        <div role="alert" className="mt-3 rounded-md border border-warn/50 bg-warn-soft px-3 py-2 text-sm">
          <p>The event stream stopped before the backend reported how the task ended. Its outcome is not known yet.</p>
          {streamError && <p className="font-mono text-xs">{streamError.code}: {streamError.message}</p>}
          <Button size="sm" variant="outline" className="mt-2" onClick={() => follow(new AbortController().signal)}>Reconnect</Button>
        </div>
      )}
    </section>
  );
}

function toneOf(status: string): Tone {
  return taskStatus(status).tone;
}

function EventRow({ e, finished }: { e: TaskEvent; finished: boolean }) {
  const t = tplus(e.t_ms);
  const step = typeof e.step === "number" ? `Step ${e.step}` : ""; // the backend numbers task steps from 1
  const row = (tone: Tone | undefined, title: React.ReactNode, body?: React.ReactNode) => (
    <li data-tone={tone}>
      <span className="t">{t}</span>
      <div className="body">
        <p className="text-sm font-medium [overflow-wrap:anywhere]">{title}</p>
        {body && <div className="mt-1 text-sm">{body}</div>}
      </div>
    </li>
  );
  switch (e.type) {
    case "task_started":
      return row(undefined, `Started (${e.arm === "plan_execute" ? "plan, then execute" : "step by step"})`,
        <p className="text-xs text-muted-foreground">{(e.offered as string[]).length} capabilities offered: {(e.offered as string[]).join(", ") || "none"}</p>);
    case "model_call":
      return row(undefined, `Model call ${e.n} (${String(e.kind)})`);
    case "plan":
      return row(e.problem ? "bad" : undefined, e.problem ? "The plan was not valid" : `Plan with ${(e.steps as unknown[] | null)?.length ?? 0} steps`,
        e.problem ? <p className="text-muted-foreground">{String(e.problem)}</p> : <JsonView value={e.steps} label="Planned steps" />);
    case "replan":
      return row("wait", `Re-planning: ${String(e.reason)}`, <p className="text-muted-foreground">{(e.problems as string[]).join("; ")}</p>);
    case "step_started":
      return row("active", <>{step}: <code>{String(e.capability)}</code></>, (
        <details>
          <summary className="cursor-pointer text-xs text-muted-foreground">Arguments{(e.targets as string[])?.length ? " and targets" : ""}</summary>
          <JsonView value={{ args: e.args, targets: e.targets }} label="Step arguments" />
        </details>
      ));
    case "step_output":
      return row("good", <>{step} returned {Number(e.bytes).toLocaleString()} bytes in {Math.round(Number(e.duration_ms))} ms</>,
        <pre tabIndex={0} aria-label={`${step} output`} className="scroll-x max-h-40 whitespace-pre-wrap rounded-sm bg-background p-2 font-mono text-xs [overflow-wrap:anywhere]">{String(e.preview)}{e.truncated ? "\n[cut to the tool-output limit]" : ""}</pre>);
    case "step_blocked":
      return row("bad", <>{step} refused: <code>{String(e.capability)}</code></>, <p><Tag>{String(e.code)}</Tag> {String(e.message)}</p>);
    case "step_failed":
      return row("bad", <>{step} failed: <code>{String(e.capability)}</code></>, <p><Tag>{String(e.code)}</Tag> {String(e.message)}</p>);
    case "step_cancelled":
      return row(undefined, <>{step} cancelled</>);
    case "approval_required":
      return <ApprovalRow e={e} t={t} finished={finished} />;
    case "approval_resolved":
      return row("good", `${step} approved by you`);
    default:
      return row(undefined, e.type);
  }
}

function ApprovalRow({ e, t, finished }: { e: TaskEvent; t: string; finished: boolean }) {
  const approval = e.approval as Approval;
  const action = useAction();
  const [decided, setDecided] = useState<string | null>(null);
  const now = useNow();
  const decide = (decision: "approve" | "deny") => action.run(async () => {
    const res = await post<{ approval: Approval }>(`/api/approvals/${approval.id}`, { decision, action_hash: approval.action_hash });
    setDecided(res.approval.status);
  });
  const open = !decided && !finished;
  return (
    <li data-tone="wait">
      <span className="t">{t}</span>
      <div className="body">
        <ApprovalDetails approval={approval} />
        {open && (
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Button size="sm" disabled={action.busy} onClick={() => decide("approve")}>Approve this action</Button>
            <Button size="sm" variant="outline" disabled={action.busy} onClick={() => decide("deny")}>Deny</Button>
            <span className="font-mono text-xs text-muted-foreground">expires in {countdown(approval.expires_at, now)}</span>
          </div>
        )}
        {decided && <p className="mt-2 text-sm">You chose: <b>{decided === "approved" ? "approve" : "deny"}</b>. The task continues once the backend applies it.</p>}
        <ErrorNote error={action.error} title="The decision was not accepted" />
      </div>
    </li>
  );
}

/** Exactly what will run: capability, arguments, targets, whether it can be undone, and the action hash. */
export function ApprovalDetails({ approval }: { approval: Approval }) {
  return (
    <div className="space-y-2">
      <p className="text-sm font-medium">
        Step {approval.step_index} wants to run <code>{approval.capability_id}</code> ({approval.capability_name})
      </p>
      <ul className="list-disc pl-5 text-sm">
        {approval.reasons.map((r) => <li key={r}>{r}</li>)}
      </ul>
      <div className="flex flex-wrap gap-1.5">
        <Tag>effect {approval.side_effects}</Tag>
        <Tag>{approval.undoable === true ? "can be undone" : approval.undoable === false ? "cannot be undone" : "undo not declared"}</Tag>
        <Tag title={approval.action_hash}>hash {shortHash(approval.action_hash)}</Tag>
      </div>
      {approval.targets.length > 0 && (
        <p className="text-sm">Targets: {approval.targets.map((t) => <code key={t} className="mr-2 [overflow-wrap:anywhere]">{t}</code>)}</p>
      )}
      <JsonView value={approval.args} label="Exact arguments" />
    </div>
  );
}
