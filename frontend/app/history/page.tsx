"use client";

import Link from "next/link";
import { useState } from "react";
import { api, del, patch, type Conversation } from "@/lib/api";
import { ago } from "@/lib/format";
import { useAction, usePoll } from "@/lib/hooks";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Confirm } from "@/components/xodus/confirm";
import { Empty, ErrorNote, Note, PageHeader } from "@/components/xodus/parts";

const PAGE = 50;

export default function History() {
  const [query, setQuery] = useState("");
  const [applied, setApplied] = useState("");
  const [limit, setLimit] = useState(PAGE);
  const list = usePoll(() => api<Conversation[]>(`/api/conversations?limit=${Math.min(limit, 200)}${applied ? `&q=${encodeURIComponent(applied)}` : ""}`),
    0, [applied, limit]);
  const action = useAction();
  const [renaming, setRenaming] = useState<Conversation | null>(null);
  const [title, setTitle] = useState("");
  const [report, setReport] = useState<string | null>(null);

  async function remove(c: Conversation) {
    const res = await action.run(() => del<{ deleted: { messages: number; memory_items: number; messages_left: number } }>(`/api/conversations/${c.id}`));
    if (res) {
      const d = res.deleted;
      setReport(`Deleted "${c.title}": ${d.messages} messages and ${d.memory_items} memory items derived from it. ` +
        (d.messages_left === 0 ? "Checked: no message of it is left." : `Warning: ${d.messages_left} messages are still present.`));
      void list.refresh();
    }
  }

  async function rename() {
    if (!renaming) return;
    const res = await action.run(() => patch(`/api/conversations/${renaming.id}`, { title }));
    if (res) {
      setRenaming(null);
      void list.refresh();
    }
  }

  const rows = list.data ?? [];
  return (
    <>
      <PageHeader number="02" title="History" lead="Every saved conversation, newest first. Deleting one also deletes the memory items derived from it." />
      <form className="mb-5 flex flex-wrap items-end gap-2" onSubmit={(e) => { e.preventDefault(); setApplied(query.trim()); setLimit(PAGE); }}>
        <div className="min-w-[14rem] flex-1">
          <Label htmlFor="q">Search titles</Label>
          <Input id="q" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Holiday plans" />
        </div>
        <Button type="submit" variant="outline">Search</Button>
        {applied && <Button type="button" variant="ghost" onClick={() => { setQuery(""); setApplied(""); }}>Show all</Button>}
      </form>

      {report && <div className="mb-4"><Note role="status">{report}</Note></div>}
      <ErrorNote error={list.error} title="Conversations cannot be loaded" />
      <ErrorNote error={action.error} title="That did not work" />

      {!list.loading && rows.length === 0 && !list.error && (
        <Empty title={applied ? `No conversation title contains "${applied}"` : "No conversations yet"}>
          {applied ? "Try another word." : <>Start one in the <Link className="underline" href="/">workspace</Link>.</>}
        </Empty>
      )}
      <ul className="space-y-2">
        {rows.map((c) => (
          <li key={c.id} className="flex flex-col gap-3 panel p-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0">
              <p className="eyebrow">updated {ago(c.updated_at)}, {c.messages} {c.messages === 1 ? "message" : "messages"}</p>
              <h2 className="mt-0.5 text-base font-semibold [overflow-wrap:anywhere]">
                <Link href={`/?c=${c.id}`} className="hover:underline">{c.title}</Link>
              </h2>
              {c.last_message && <p className="mt-0.5 line-clamp-2 text-sm text-muted-foreground [overflow-wrap:anywhere]">{c.last_message}</p>}
            </div>
            <div className="flex shrink-0 gap-2">
              <Button variant="outline" size="sm" asChild><Link href={`/?c=${c.id}`} aria-label={`Open ${c.title}`}>Open</Link></Button>
              <Button variant="ghost" size="sm" onClick={() => { setRenaming(c); setTitle(c.title); }} aria-label={`Rename ${c.title}`}>Rename</Button>
              <Confirm title={`Delete "${c.title}"?`} confirmLabel="Delete conversation"
                description={<>This removes its {c.messages} messages and any memory items derived from it, from the database file itself. It cannot be undone.</>}
                onConfirm={() => void remove(c)}
                trigger={<Button variant="destructive" size="sm" aria-label={`Delete ${c.title}`}>Delete</Button>} />
            </div>
          </li>
        ))}
      </ul>
      {rows.length >= limit && limit < 200 && (
        <Button variant="outline" className="mt-4" onClick={() => setLimit((l) => l + PAGE)}>Show more</Button>
      )}

      <Dialog open={renaming !== null} onOpenChange={(o) => !o && setRenaming(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Rename conversation</DialogTitle>
            <DialogDescription>Only the title changes; the messages stay as they are.</DialogDescription>
          </DialogHeader>
          <form onSubmit={(e) => { e.preventDefault(); void rename(); }} className="space-y-2">
            <Label htmlFor="title">Title</Label>
            <Input id="title" value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} />
            <DialogFooter>
              <Button type="submit" disabled={!title.trim() || action.busy}>Save title</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}
