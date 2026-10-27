"use client";

import { useState } from "react";
import { api, ApiError, del, post } from "@/lib/api";
import { ago, bytes } from "@/lib/format";
import { useAction, usePoll } from "@/lib/hooks";
import { JOB_STATUS } from "@/lib/status";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Confirm } from "@/components/xodus/confirm";
import { Empty, ErrorNote, Note, PageHeader, Section, Status, Tag, Ticket } from "@/components/xodus/parts";

type Collection = { id: number; name: string; description: string; documents: number; documents_ready: number; chunks: number };
type Job = { id: number; collection: string; root_id: string; rel_path: string; status: string; error: string | null; error_code: string | null;
  attempts: number; chunks_done: number; outcome: string | null; created_at: number; cancel_requested: number };
type Doc = { id: number; collection: string; title: string; source_root: string; source_path: string; format: string; size_bytes: number;
  status: string; error: string | null; chunks_done: number; pages: number | null; updated_at: number };
type Stats = { documents: Record<string, number>; chunks: number; chunk_limit: number; file_bytes: { db: number; wal: number };
  ingestion: { paused: boolean; pending_jobs: number; worker: { running: boolean; degraded: string | null } } };
type Settings = { roots: { id: string; path: string; grants: string[] }[] };
type Verification = { deleted: { chunks: number; vectors: number; memory_items: number }; verification: { clean: boolean } };
type IngestResult = { jobs: Job[]; rejected: { path: string; code: string; reason: string }[]; paused: boolean };

export default function Knowledge() {
  const stats = usePoll(() => api<Stats>("/api/knowledge/stats"), 4000);
  const collections = usePoll(() => api<Collection[]>("/api/knowledge/collections?limit=200"), 0);
  const settings = usePoll(() => api<Settings>("/api/settings"), 0);
  const active = (stats.data?.ingestion.pending_jobs ?? 0) > 0;
  const jobs = usePoll(() => api<Job[]>("/api/knowledge/jobs?limit=30"), active ? 1500 : 5000, [active]);
  const [filter, setFilter] = useState("");
  const docs = usePoll(() => api<Doc[]>(`/api/knowledge/documents?limit=200${filter ? `&collection=${filter}` : ""}`), active ? 3000 : 0, [filter, active]);
  const action = useAction();
  const [notice, setNotice] = useState<string | null>(null);

  const refreshAll = () => { void collections.refresh(); void jobs.refresh(); void docs.refresh(); void stats.refresh(); };
  const s = stats.data;
  const readableRoots = (settings.data?.roots ?? []).filter((r) => r.grants.includes("fs.read"));

  return (
    <>
      <PageHeader number="03" title="Knowledge" lead="Documents XODUS can search and cite. Files are read from the folders in your configuration and never leave this computer." />
      {s?.ingestion.paused && (
        <div className="mb-4"><Note tone="wait" role="status">
          Ingestion is paused: {s.ingestion.pending_jobs} job(s) wait in the queue and resume from their checkpoint.{" "}
          <Button size="sm" variant="outline" className="ml-1" onClick={() => action.run(async () => { await post("/api/ingestion/resume"); refreshAll(); })}>Resume ingestion</Button>
        </Note></div>
      )}
      {s?.ingestion.worker.degraded && <ErrorNote error={{ code: "ingestion_degraded", message: s.ingestion.worker.degraded }} title="The ingestion worker keeps stopping" />}
      {s && <Ticket className="mb-6 mt-0" fields={[["documents ready", s.documents.ready ?? 0], ["failed", s.documents.failed ?? 0],
        ["passages", `${s.chunks.toLocaleString()} of ${s.chunk_limit.toLocaleString()}`], ["database", bytes(s.file_bytes.db + s.file_bytes.wal)],
        ["worker", s.ingestion.worker.running ? "running" : "stopped (starts when needed)"]]} />}
      {notice && <div className="mb-4"><Note role="status">{notice}</Note></div>}
      <ErrorNote error={action.error} title="That did not work" />

      <div className="grid gap-6 lg:grid-cols-2">
        <CollectionsPanel collections={collections.data ?? []} error={collections.error} onChange={refreshAll} setNotice={setNotice} />
        <IngestPanel collections={collections.data ?? []} roots={readableRoots} onQueued={(msg) => { setNotice(msg); refreshAll(); }} />
      </div>

      <Section title="Ingestion jobs" id="jobs" description="One file at a time. A stopped worker resumes from the last saved batch.">
        <ErrorNote error={jobs.error} />
        {(jobs.data ?? []).length === 0 ? <Empty title="No ingestion jobs yet" /> : (
          <ol className="space-y-2">
            {(jobs.data ?? []).map((j) => {
              const st = JOB_STATUS[j.status] ?? { label: j.status, tone: "neutral" as const };
              return (
                <li key={j.id} className="flex flex-col gap-2 rounded-md border border-border bg-card p-3 sm:flex-row sm:items-center sm:justify-between">
                  <div className="min-w-0">
                    <p className="eyebrow">job {j.id}, {j.collection}, queued {ago(j.created_at)}</p>
                    <p className="font-mono text-sm [overflow-wrap:anywhere]">{j.root_id}/{j.rel_path}</p>
                    {j.error && <p className="text-sm text-destructive">{j.error}</p>}
                  </div>
                  <div className="flex shrink-0 flex-wrap items-center gap-2">
                    <Status tone={st.tone}>{j.cancel_requested && j.status === "running" ? "Stopping at next checkpoint" : st.label}</Status>
                    <Tag>{j.chunks_done} passages</Tag>
                    {j.attempts > 1 && <Tag>attempt {j.attempts}</Tag>}
                    {j.outcome && j.outcome !== "ingested" && <Tag>{j.outcome}</Tag>}
                    {(j.status === "queued" || j.status === "running") && !j.cancel_requested && (
                      <Button size="sm" variant="outline" onClick={() => action.run(async () => { await post(`/api/knowledge/jobs/${j.id}/cancel`); refreshAll(); })}>
                        Cancel job
                      </Button>
                    )}
                  </div>
                </li>
              );
            })}
          </ol>
        )}
      </Section>

      <Section title="Documents" id="documents" actions={
        <div className="flex items-center gap-2">
          <Label htmlFor="docfilter" className="text-sm">Collection</Label>
          <select id="docfilter" value={filter} onChange={(e) => setFilter(e.target.value)} className="h-8 rounded-md border border-input bg-background px-2 text-sm">
            <option value="">All</option>
            {(collections.data ?? []).map((c) => <option key={c.id} value={c.name}>{c.name}</option>)}
          </select>
        </div>}>
        <ErrorNote error={docs.error} />
        {(docs.data ?? []).length === 0 ? <Empty title="No documents">Add files above; they appear here once ingestion starts.</Empty> : (
          <ul className="space-y-2">
            {(docs.data ?? []).map((d) => <DocumentRow key={d.id} d={d} onDeleted={(msg) => { setNotice(msg); refreshAll(); }} />)}
          </ul>
        )}
      </Section>
    </>
  );
}

function CollectionsPanel({ collections, error, onChange, setNotice }: {
  collections: Collection[]; error: ApiError | null; onChange: () => void; setNotice: (s: string) => void;
}) {
  const [name, setName] = useState("");
  const action = useAction();
  return (
    <section aria-labelledby="collections" className="rounded-md border border-border bg-card p-4">
      <h2 id="collections" className="text-lg font-semibold">Collections</h2>
      <p className="text-sm text-muted-foreground">Groups of documents you search, grant to the model, or delete together.</p>
      <form className="mt-3 flex flex-wrap items-end gap-2" onSubmit={async (e) => {
        e.preventDefault();
        if (await action.run(() => post("/api/knowledge/collections", { name }))) { setName(""); onChange(); }
      }}>
        <div className="min-w-[10rem] flex-1">
          <Label htmlFor="cname">New collection name</Label>
          <Input id="cname" value={name} onChange={(e) => setName(e.target.value.toLowerCase())} placeholder="notes" pattern="[a-z][a-z0-9_-]{0,31}"
            aria-describedby="cname-help" />
          <p id="cname-help" className="mt-1 text-xs text-muted-foreground">Lowercase letters, digits, - and _.</p>
        </div>
        <Button type="submit" disabled={!name || action.busy}>Create collection</Button>
      </form>
      <ErrorNote error={action.error ?? error} />
      <ul className="mt-3 divide-y divide-border">
        {collections.length === 0 && <li className="py-2 text-sm text-muted-foreground">None yet.</li>}
        {collections.map((c) => (
          <li key={c.id} className="flex items-center justify-between gap-2 py-2">
            <div className="min-w-0">
              <p className="font-medium">{c.name}</p>
              <p className="font-mono text-xs text-muted-foreground">{c.documents_ready} ready of {c.documents} documents, {c.chunks.toLocaleString()} passages</p>
            </div>
            <Confirm title={`Delete the collection "${c.name}"?`} confirmLabel="Delete collection"
              description={`All ${c.documents} documents in it, their passages, search indexes and derived memory items are removed from the database file. The original files stay where they are.`}
              onConfirm={async () => {
                const r = await action.run(() => del<{ deleted: { documents: number; chunks: number; vectors: number; memory_items: number } }>(`/api/knowledge/collections/${c.id}`));
                if (r) { setNotice(`Deleted "${c.name}": ${r.deleted.documents} documents, ${r.deleted.chunks} passages, ${r.deleted.vectors} vectors, ${r.deleted.memory_items} memory items.`); onChange(); }
              }}
              trigger={<Button variant="destructive" size="sm" aria-label={`Delete collection ${c.name}`}>Delete</Button>} />
          </li>
        ))}
      </ul>
    </section>
  );
}

function IngestPanel({ collections, roots, onQueued }: {
  collections: Collection[]; roots: Settings["roots"]; onQueued: (msg: string) => void;
}) {
  const [collection, setCollection] = useState("");
  const [root, setRoot] = useState("");
  const [path, setPath] = useState(".");
  const [recursive, setRecursive] = useState(false);
  const [rejected, setRejected] = useState<{ path: string; code: string; reason: string }[]>([]);
  const action = useAction();
  const coll = collection || collections[0]?.name || "";
  const rootId = root || roots[0]?.id || "";

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setRejected([]);
    const res = await action.run(async () => {
      try {
        return await post<IngestResult>("/api/knowledge/ingest", { collection: coll, root: rootId, path, recursive });
      } catch (err) {
        // 422 with a "rejected" list is a normal answer: nothing could be queued, and here is why for each file
        if (err instanceof ApiError && err.status === 422 && Array.isArray((err.body as IngestResult | null)?.rejected)) return err.body as IngestResult;
        throw err;
      }
    });
    if (res) {
      setRejected(res.rejected ?? []);
      onQueued(`${res.jobs.length} file(s) queued${res.rejected?.length ? `, ${res.rejected.length} skipped (reasons below)` : ""}.${res.paused ? " Ingestion is paused: they start when you resume it." : ""}`);
    }
  }

  if (!collections.length) {
    return (
      <section className="rounded-md border border-border bg-card p-4">
        <h2 className="text-lg font-semibold">Add files</h2>
        <p className="text-sm text-muted-foreground">Create a collection first; files are added to one.</p>
      </section>
    );
  }
  return (
    <section aria-labelledby="add-files" className="rounded-md border border-border bg-card p-4">
      <h2 id="add-files" className="text-lg font-semibold">Add files</h2>
      <p className="text-sm text-muted-foreground">Markdown, text, CSV, PDF (with a text layer) and DOCX, up to 50 MB each. Links and junctions are never followed.</p>
      <form onSubmit={submit} className="mt-3 grid gap-3 sm:grid-cols-2">
        <div>
          <Label htmlFor="icoll">Collection</Label>
          <select id="icoll" value={coll} onChange={(e) => setCollection(e.target.value)} className="h-8 w-full rounded-md border border-input bg-background px-2 text-sm">
            {collections.map((c) => <option key={c.id} value={c.name}>{c.name}</option>)}
          </select>
        </div>
        <div>
          <Label htmlFor="iroot">Folder</Label>
          <select id="iroot" value={rootId} onChange={(e) => setRoot(e.target.value)} className="h-8 w-full rounded-md border border-input bg-background px-2 text-sm">
            {roots.map((r) => <option key={r.id} value={r.id}>{r.id}</option>)}
          </select>
        </div>
        <div className="sm:col-span-2">
          <Label htmlFor="ipath">File or subfolder (relative to the folder)</Label>
          <Input id="ipath" value={path} onChange={(e) => setPath(e.target.value)} />
          {roots.find((r) => r.id === rootId) && <p className="mt-1 font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]">{roots.find((r) => r.id === rootId)!.path}</p>}
        </div>
        <label className="flex items-center gap-2 text-sm sm:col-span-2">
          <input type="checkbox" checked={recursive} onChange={(e) => setRecursive(e.target.checked)} className="size-4 accent-[var(--primary)]" />
          Include subfolders
        </label>
        <div className="sm:col-span-2">
          <Button type="submit" disabled={action.busy || !rootId}>Queue for ingestion</Button>
        </div>
      </form>
      {roots.length === 0 && <p className="mt-2 text-sm text-muted-foreground">No folder is configured for reading. Add one under <code>[[permissions.roots]]</code> in config/local.toml.</p>}
      <ErrorNote error={action.error} title="Nothing was queued" />
      {rejected.length > 0 && (
        <div className="mt-3">
          <p className="text-sm font-medium">Skipped</p>
          <ul className="mt-1 space-y-1 text-sm">
            {rejected.map((r) => <li key={r.path}><code className="[overflow-wrap:anywhere]">{r.path}</code>: {r.reason} <Tag>{r.code}</Tag></li>)}
          </ul>
        </div>
      )}
    </section>
  );
}

function DocumentRow({ d, onDeleted }: { d: Doc; onDeleted: (msg: string) => void }) {
  const [open, setOpen] = useState(false);
  const action = useAction();
  const chunks = usePoll(() => open ? api<{ id: number; ordinal: number; text: string; locator: string | null; heading_path: string | null }[]>(`/api/knowledge/documents/${d.id}/chunks?limit=50`) : Promise.resolve(null), 0, [open]);
  const tone = d.status === "ready" ? "good" : d.status === "failed" ? "bad" : d.status === "ingesting" ? "active" : "neutral";
  return (
    <li className="rounded-md border border-border bg-card p-3">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <p className="eyebrow">{d.collection}, {d.format}, {bytes(d.size_bytes)}{d.pages ? `, ${d.pages} pages` : ""}</p>
          <p className="font-medium [overflow-wrap:anywhere]">{d.title}</p>
          <p className="font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]">{d.source_root}/{d.source_path}</p>
          {d.error && <p className="text-sm text-destructive">{d.error}</p>}
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          <Status tone={tone}>{d.status === "ready" ? "Ready" : d.status === "ingesting" ? "Ingesting" : d.status === "failed" ? "Failed" : "Cancelled"}</Status>
          <Tag>{d.chunks_done} passages</Tag>
          <Button size="sm" variant="outline" onClick={() => setOpen(true)} disabled={!d.chunks_done}>View passages</Button>
          <Confirm title={`Delete "${d.title}"?`} confirmLabel="Delete document"
            description="Its passages, keyword and vector index entries, and memory items derived from it are removed from the database file, and XODUS checks that nothing is left. The original file is not touched."
            onConfirm={async () => {
              const r = await action.run(() => del<Verification>(`/api/knowledge/documents/${d.id}`));
              if (r) onDeleted(`Deleted "${d.title}": ${r.deleted.chunks} passages, ${r.deleted.vectors} vectors, ${r.deleted.memory_items} memory items. ` +
                (r.verification.clean ? "Verified: nothing of it is left in the database." : "Warning: the check found remaining entries; see the audit log."));
            }}
            trigger={<Button size="sm" variant="destructive" aria-label={`Delete document ${d.title}`}>Delete</Button>} />
        </div>
      </div>
      <ErrorNote error={action.error} title="Not deleted" />
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>{d.title}</DialogTitle>
            <DialogDescription>The first 50 stored passages, exactly as the search and the model see them.</DialogDescription>
          </DialogHeader>
          <ErrorNote error={chunks.error} />
          <ol className="space-y-3">
            {(chunks.data ?? []).map((c) => (
              <li key={c.id}>
                <p className="eyebrow">passage {c.ordinal + 1}, id {c.id}{c.locator ? `, ${c.locator}` : ""}{c.heading_path ? `, ${c.heading_path}` : ""}</p>
                <p className="prose-reply text-sm">{c.text}</p>
              </li>
            ))}
          </ol>
        </DialogContent>
      </Dialog>
    </li>
  );
}
