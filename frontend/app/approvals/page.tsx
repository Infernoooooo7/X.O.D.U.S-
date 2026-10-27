"use client";

import Link from "next/link";
import { api, post, type Approval } from "@/lib/api";
import { ago, countdown } from "@/lib/format";
import { useAction, useNow, usePoll } from "@/lib/hooks";
import { APPROVAL_STATUS } from "@/lib/status";
import { Button } from "@/components/ui/button";
import { ApprovalDetails } from "@/components/xodus/task-timeline";
import { Empty, ErrorNote, Note, PageHeader, Section, Status, Ticket } from "@/components/xodus/parts";

export default function Approvals() {
  const pending = usePoll(() => api<Approval[]>("/api/approvals"), 2000);
  const all = usePoll(() => api<Approval[]>("/api/approvals?status=all"), 5000);
  const decided = (all.data ?? []).filter((a) => a.status !== "pending").sort((a, b) => b.created_at - a.created_at).slice(0, 30);

  return (
    <>
      <PageHeader number="06" title="Approvals" lead="Actions a task wants to take that change something. The task waits until you decide; each approval works once, for exactly the action shown." />
      <Note>
        Approving sends back the fingerprint (hash) of the action you saw. If the task tries anything different, even one changed
        argument, the approval does not apply and the step is refused.
      </Note>
      <ErrorNote error={pending.error} title="Approvals cannot be loaded" />

      <Section title="Waiting for you" id="pending">
        {(pending.data ?? []).length === 0 ? (
          <Empty title="Nothing is waiting">When a task needs to write a file or do anything with an effect, it pauses and appears here.</Empty>
        ) : (
          <ol className="space-y-4">
            {(pending.data ?? []).map((a, i) => <PendingCard key={a.id} a={a} n={i + 1} onDone={() => { void pending.refresh(); void all.refresh(); }} />)}
          </ol>
        )}
      </Section>

      <Section title="Recent decisions" id="decided">
        {decided.length === 0 ? <Empty title="No decisions yet" /> : (
          <ul className="space-y-2">
            {decided.map((a) => {
              const st = APPROVAL_STATUS[a.status] ?? { label: a.status, tone: "neutral" as const };
              return (
                <li key={a.id} className="flex flex-col gap-1 rounded-md border border-border bg-card p-3 text-sm sm:flex-row sm:items-center sm:justify-between">
                  <span className="min-w-0"><code>{a.capability_id}</code> for task <code>{a.task_id}</code>, step {a.step_index}, {ago(a.created_at)}</span>
                  <Status tone={st.tone}>{st.label}</Status>
                </li>
              );
            })}
          </ul>
        )}
      </Section>
    </>
  );
}

function PendingCard({ a, n, onDone }: { a: Approval; n: number; onDone: () => void }) {
  const action = useAction();
  const now = useNow();
  const decide = async (decision: "approve" | "deny") => {
    if (await action.run(() => post(`/api/approvals/${a.id}`, { decision, action_hash: a.action_hash }))) onDone();
  };
  const left = countdown(a.expires_at, now);
  return (
    <li className="rounded-md border border-warn/50 bg-card p-4">
      <div className="flex items-baseline gap-3">
        <span className="card-number">{String(n).padStart(2, "0")}</span>
        <span className="eyebrow">task {a.task_id}</span>
      </div>
      <div className="mt-2"><ApprovalDetails approval={a} /></div>
      <Ticket fields={[["requested", ago(a.created_at, now)], ["expires in", left], ["capability", `${a.capability_id} v${a.capability_version}`]]} />
      <div className="mt-3 flex flex-wrap gap-2">
        <Button disabled={action.busy || left === "expired"} onClick={() => decide("approve")}>Approve this action</Button>
        <Button variant="outline" disabled={action.busy} onClick={() => decide("deny")}>Deny</Button>
        <Button variant="ghost" asChild><Link href="/activity/">See the task</Link></Button>
      </div>
      <ErrorNote error={action.error} title="The decision was not accepted" />
    </li>
  );
}
