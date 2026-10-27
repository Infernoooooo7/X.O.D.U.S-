"use client";

import { useState } from "react";
import { api, type Task } from "@/lib/api";
import { ago } from "@/lib/format";
import { useAction, usePoll } from "@/lib/hooks";
import { taskStatus } from "@/lib/status";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Empty, ErrorNote, Note, PageHeader, Section, Status, Tag } from "@/components/xodus/parts";
import { TaskTimeline } from "@/components/xodus/task-timeline";

type Record_ = { seq: number; ts: string; event: string; hash: string; [key: string]: unknown };
type AuditPage = { records: Record_[]; head_seq: number; next_before: number | null };
type Verify = { ok: boolean; records: number; first_bad_seq: number | null; reason: string | null };

const GROUPS: [string, string][] = [
  ["", "All events"], ["capability_", "Capability calls"], ["approval_", "Approvals"], ["task_", "Tasks"], ["model_", "Models"],
  ["memory_", "Memory"], ["document_", "Documents"], ["collection_", "Collections"], ["conversation_", "Conversations"],
  ["ingest", "Ingestion"], ["process_", "Processes"],
];
const SKIP = new Set(["seq", "ts", "event", "prev", "hash"]);

export default function Activity() {
  const tasks = usePoll(() => api<Task[]>("/api/tasks"), 3000);
  const [open, setOpen] = useState<string | null>(null);
  const [group, setGroup] = useState("");
  const [pages, setPages] = useState<Record_[][]>([]);
  // The newest page refreshes itself, but not once older pages are loaded: that would open a gap between them.
  const first = usePoll(() => api<AuditPage>(`/api/audit?limit=50${group ? `&event=${group}` : ""}`), pages.length ? 0 : 5000, [group]);
  const older = useAction();
  const verify = useAction();
  const [verified, setVerified] = useState<Verify | null>(null);

  const records = [...(first.data?.records ?? []), ...pages.flat()];
  const nextBefore = pages.length ? (pages[pages.length - 1].length === 50 ? pages[pages.length - 1][49].seq : null) : first.data?.next_before ?? null;

  return (
    <>
      <PageHeader number="07" title="Activity" lead="What XODUS did and why: recent tasks with every step, and the append-only audit log." />

      <Section title="Recent tasks" id="tasks" description="Kept in memory for this backend session (the last 50); every task event is also written to logs/tasks.jsonl.">
        <ErrorNote error={tasks.error} />
        {(tasks.data ?? []).length === 0 ? <Empty title="No tasks in this session">Start one in the workspace with Run task.</Empty> : (
          <ul className="space-y-2">
            {(tasks.data ?? []).map((t) => {
              const st = taskStatus(t.status);
              return (
                <li key={t.id} className="panel p-3">
                  <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                    <div className="min-w-0">
                      <p className="eyebrow">task {t.id}, {t.arm === "plan_execute" ? "plan first" : "step by step"}, {ago(t.created_at)}</p>
                      <p className="font-medium [overflow-wrap:anywhere]">{t.goal}</p>
                    </div>
                    <div className="flex shrink-0 flex-wrap items-center gap-2">
                      <Status tone={st.tone} title={st.explain}>{st.label}</Status>
                      <Tag>{t.steps.length} steps</Tag>
                      {t.tainted && <Tag title="The task read untrusted content (files, tool output), so later side effects needed approval">read untrusted content</Tag>}
                      <Button size="sm" variant="outline" aria-expanded={open === t.id} onClick={() => setOpen(open === t.id ? null : t.id)}>
                        {open === t.id ? "Hide timeline" : "Show timeline"}
                      </Button>
                    </div>
                  </div>
                  {open === t.id && <div className="mt-3"><TaskTimeline taskId={t.id} /></div>}
                </li>
              );
            })}
          </ul>
        )}
      </Section>

      <Section title="Audit log" id="audit" description="Hash-chained: editing, removing or reordering an earlier record is detected by the check."
        actions={<>
          <span className="flex items-center gap-1.5 text-sm">
            <Label htmlFor="group">Show</Label>
            <select id="group" value={group} onChange={(e) => { setGroup(e.target.value); setPages([]); }}
              className="h-8 rounded-lg border border-input bg-background px-2 text-sm">
              {GROUPS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </span>
          <Button variant="outline" size="sm" disabled={verify.busy} onClick={async () => {
            const r = await verify.run(() => api<Verify>("/api/audit/verify"));
            if (r) setVerified(r);
          }}>{verify.busy ? "Checking…" : "Verify chain"}</Button>
        </>}>
        {verified && (
          <div className="mb-3">
            {verified.ok
              ? <Note role="status">The chain is intact: {verified.records.toLocaleString()} records checked, none edited, removed or reordered.</Note>
              : <ErrorNote error={{ code: "audit_chain_broken", message: `Record ${verified.first_bad_seq}: ${verified.reason}` }} title="The audit log was changed outside XODUS" />}
          </div>
        )}
        <ErrorNote error={first.error ?? verify.error ?? older.error} />
        {records.length === 0 && !first.loading ? <Empty title="No audit records match" /> : (
          <ol className="divide-y divide-border panel">
            {records.map((r) => (
              <li key={r.seq} className="grid gap-1 px-3 py-2 text-sm sm:grid-cols-[5rem_11rem_1fr] sm:gap-3">
                <span className="font-mono text-xs text-muted-foreground">#{r.seq}</span>
                <span className="font-mono text-xs">{r.event}</span>
                <span className="min-w-0 text-xs text-muted-foreground [overflow-wrap:anywhere]">
                  <time dateTime={r.ts} className="mr-2">{r.ts.replace("T", " ").slice(0, 19)}</time>
                  {Object.entries(r).filter(([k]) => !SKIP.has(k)).map(([k, v]) => (
                    <span key={k} className="mr-2"><span className="text-foreground">{k}</span> {typeof v === "string" ? v : JSON.stringify(v)}</span>
                  ))}
                </span>
              </li>
            ))}
          </ol>
        )}
        {nextBefore !== null && (
          <Button variant="outline" className="mt-3" disabled={older.busy} onClick={async () => {
            const page = await older.run(() => api<AuditPage>(`/api/audit?limit=50&before=${nextBefore}${group ? `&event=${group}` : ""}`));
            if (page) setPages((p) => [...p, page.records]);
          }}>Older records</Button>
        )}
      </Section>
    </>
  );
}
