"use client";

import { useState } from "react";
import { api, del, patch, post } from "@/lib/api";
import { ago } from "@/lib/format";
import { useAction, usePoll } from "@/lib/hooks";
import type { Tone } from "@/lib/status";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Confirm } from "@/components/xodus/confirm";
import { Empty, ErrorNote, Note, PageHeader, Section, Status, Tag, Ticket } from "@/components/xodus/parts";

type Item = { id: number; kind: string; content: string; status: "proposed" | "approved" | "rejected"; created_by: "user" | "model";
  source: string; model: string | null; prompt_version: string | null; edited_by_user: number; created_at: number; decided_at: number | null };

const STATUS: Record<string, { label: string; tone: Tone }> = {
  proposed: { label: "Proposed by a model: not used yet", tone: "wait" },
  approved: { label: "Approved", tone: "good" },
  rejected: { label: "Rejected", tone: "neutral" },
};

export default function Memory() {
  const [status, setStatus] = useState("");
  const [kind, setKind] = useState("");
  const [creator, setCreator] = useState("");
  const query = new URLSearchParams({ limit: "200", ...(status && { status }), ...(kind && { kind }), ...(creator && { created_by: creator }) });
  const items = usePoll(() => api<Item[]>(`/api/memory/items?${query}`), 0, [status, kind, creator]);
  const proposed = usePoll(() => api<Item[]>("/api/memory/items?status=proposed&limit=200"), 0);
  const action = useAction();
  const [newKind, setNewKind] = useState("note");
  const [content, setContent] = useState("");
  const [editing, setEditing] = useState<Item | null>(null);
  const [draft, setDraft] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = () => { void items.refresh(); void proposed.refresh(); };
  const act = async (fn: () => Promise<unknown>, msg?: string) => {
    const ok = await action.run(async () => { await fn(); return true; });
    if (ok) { setNotice(msg ?? null); refresh(); }
  };

  async function exportAll() {
    const data = await action.run(() => api<unknown>("/api/memory/export"));
    if (!data) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: `xodus-memory-${new Date().toISOString().slice(0, 10)}.json` });
    a.click();
    URL.revokeObjectURL(url);
    setNotice("Exported every memory item to a JSON file in your downloads.");
  }

  const pendingCount = proposed.data?.length ?? 0;
  return (
    <>
      <PageHeader number="04" title="Memory" lead="What XODUS may remember about you. Items a model suggests stay proposed until you approve them; only approved items are ever used.">
        <Button variant="outline" onClick={exportAll} disabled={action.busy}>Export all as JSON</Button>
      </PageHeader>
      {pendingCount > 0 && status !== "proposed" && (
        <div className="mb-4"><Note tone="wait" role="status">
          {pendingCount} item(s) proposed by a model are waiting for your decision.{" "}
          <Button size="sm" variant="outline" onClick={() => setStatus("proposed")}>Show them</Button>
        </Note></div>
      )}
      {notice && <div className="mb-4"><Note role="status">{notice}</Note></div>}
      <ErrorNote error={action.error} title="That did not work" />

      <section aria-labelledby="add" className="rounded-md border border-border bg-card p-4">
        <h2 id="add" className="text-lg font-semibold">Add something to remember</h2>
        <form className="mt-3 grid gap-3 sm:grid-cols-[10rem_1fr_auto] sm:items-end" onSubmit={(e) => {
          e.preventDefault();
          void act(async () => { await post("/api/memory/items", { kind: newKind, content }); setContent(""); }, "Saved. Items you add yourself are approved at once.");
        }}>
          <div>
            <Label htmlFor="mkind">Kind</Label>
            <select id="mkind" value={newKind} onChange={(e) => setNewKind(e.target.value)} className="h-8 w-full rounded-md border border-input bg-background px-2 text-sm">
              <option value="note">Note</option><option value="fact">Fact</option><option value="preference">Preference</option>
            </select>
          </div>
          <div>
            <Label htmlFor="mcontent">Text</Label>
            <Textarea id="mcontent" rows={2} maxLength={10000} value={content} onChange={(e) => setContent(e.target.value)} placeholder="I prefer short answers." />
          </div>
          <Button type="submit" disabled={!content.trim() || action.busy}>Save item</Button>
        </form>
      </section>

      <Section title="Items" id="items" actions={
        <div className="flex flex-wrap gap-2 text-sm">
          <Filter id="fstatus" label="Status" value={status} onChange={setStatus} options={[["", "Any"], ["proposed", "Proposed"], ["approved", "Approved"], ["rejected", "Rejected"]]} />
          <Filter id="fkind" label="Kind" value={kind} onChange={setKind} options={[["", "Any"], ["note", "Note"], ["fact", "Fact"], ["preference", "Preference"]]} />
          <Filter id="fby" label="Created by" value={creator} onChange={setCreator} options={[["", "Anyone"], ["user", "You"], ["model", "A model"]]} />
        </div>}>
        <ErrorNote error={items.error} />
        {(items.data ?? []).length === 0 && !items.loading ? <Empty title="No memory items match" /> : (
          <ul className="space-y-2">
            {(items.data ?? []).map((m) => {
              const st = STATUS[m.status];
              return (
                <li key={m.id} className="rounded-md border border-border bg-card p-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <p className="eyebrow">{m.kind} {m.id}</p>
                    <Status tone={st.tone}>{st.label}</Status>
                    {m.edited_by_user ? <Tag>edited by you</Tag> : null}
                  </div>
                  <p className="prose-reply mt-1">{m.content}</p>
                  <Ticket fields={[["by", m.created_by === "user" ? "you" : "model"], ["source", m.source], ["model", m.model ?? ""],
                    ["prompt", m.prompt_version ?? ""], ["created", ago(m.created_at)], ["decided", m.decided_at ? ago(m.decided_at) : ""]]} />
                  <div className="mt-3 flex flex-wrap gap-2">
                    {m.status !== "approved" && (
                      <Button size="sm" onClick={() => void act(() => post(`/api/memory/items/${m.id}/approve`))}>Approve</Button>
                    )}
                    {m.status !== "rejected" && (
                      <Button size="sm" variant="outline" onClick={() => void act(() => post(`/api/memory/items/${m.id}/reject`))}>Reject</Button>
                    )}
                    <Button size="sm" variant="ghost" onClick={() => { setEditing(m); setDraft(m.content); }}>Edit</Button>
                    <Confirm title="Delete this memory item?" confirmLabel="Delete item" description={<>&ldquo;{m.content.slice(0, 160)}&rdquo; will be removed for good.</>}
                      onConfirm={() => void act(() => del(`/api/memory/items/${m.id}`), "Deleted.")}
                      trigger={<Button size="sm" variant="destructive" aria-label={`Delete memory item ${m.id}`}>Delete</Button>} />
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </Section>

      <Dialog open={editing !== null} onOpenChange={(o) => !o && setEditing(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Edit memory item</DialogTitle>
            <DialogDescription>Editing does not change whether it is approved; it is marked as edited by you.</DialogDescription>
          </DialogHeader>
          <form onSubmit={(e) => { e.preventDefault(); if (editing) void act(async () => { await patch(`/api/memory/items/${editing.id}`, { content: draft }); setEditing(null); }); }}>
            <Label htmlFor="mdraft">Text</Label>
            <Textarea id="mdraft" rows={4} value={draft} maxLength={10000} onChange={(e) => setDraft(e.target.value)} />
            <DialogFooter className="mt-3"><Button type="submit" disabled={!draft.trim() || action.busy}>Save changes</Button></DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}

function Filter({ id, label, value, onChange, options }: { id: string; label: string; value: string; onChange: (v: string) => void; options: [string, string][] }) {
  return (
    <span className="flex items-center gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      <select id={id} value={value} onChange={(e) => onChange(e.target.value)} className="h-8 rounded-md border border-input bg-background px-2 text-sm">
        {options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
      </select>
    </span>
  );
}
