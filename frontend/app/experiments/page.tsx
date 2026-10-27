"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import { ago, bytes } from "@/lib/format";
import { useAction, usePoll } from "@/lib/hooks";
import { Button } from "@/components/ui/button";
import { Empty, ErrorNote, Note, PageHeader, Section, Tag } from "@/components/xodus/parts";

type Run = { name: string; files: { path: string; name: string; bytes: number; modified: number; viewable: boolean }[] };
type Listing = { runs: Run[]; preregistration: string; note: string };
type FileText = { path: string; bytes: number; truncated: boolean; text: string };

export default function Experiments() {
  const list = usePoll(() => api<Listing>("/api/experiments"), 0);
  const [file, setFile] = useState<FileText | null>(null);
  const action = useAction();

  async function open(path: string) {
    const r = await action.run(() => api<FileText>(`/api/experiments/file?path=${encodeURIComponent(path)}`));
    if (r) setFile(r);
  }

  const pretty = (f: FileText) => {
    if (f.path.endsWith(".json") && !f.truncated) {
      try { return JSON.stringify(JSON.parse(f.text), null, 2); } catch { return f.text; }
    }
    return f.text;
  };

  return (
    <>
      <PageHeader number="10" title="Experiments" lead="Measurement and evaluation results written by XODUS's scripts. Read-only here; nothing is run from this page." />
      <Note>Pre-registration: {list.data?.preregistration ?? "…"}. {list.data?.note}</Note>
      <ErrorNote error={list.error ?? action.error} />
      <div className="mt-6 grid gap-6 lg:grid-cols-[22rem_1fr]">
        <div className="space-y-3">
          {(list.data?.runs ?? []).length === 0 && !list.loading && <Empty title="No results yet">Results appear after a benchmark or measurement script runs.</Empty>}
          {(list.data?.runs ?? []).map((run) => (
            <section key={run.name} aria-label={run.name} className="rounded-md border border-border bg-card p-3">
              <h2 className="font-mono text-sm font-semibold">{run.name}</h2>
              <ul className="mt-2 space-y-1">
                {run.files.map((f) => (
                  <li key={f.path} className="flex items-center justify-between gap-2 text-sm">
                    {f.viewable
                      ? <Button variant="link" size="sm" className="h-auto p-0" onClick={() => open(f.path)}>{f.name}</Button>
                      : <span className="text-muted-foreground">{f.name}</span>}
                    <span className="shrink-0 font-mono text-xs text-muted-foreground">{bytes(f.bytes)}, {ago(f.modified)}</span>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
        <Section title={file ? file.path : "Choose a file"} id="viewer" description={file ? undefined : "Markdown summaries and JSON results open here as text."}>
          {file && (
            <>
              <div className="mb-2 flex gap-2"><Tag>{bytes(file.bytes)}</Tag>{file.truncated && <Tag>first 1 MB shown</Tag>}</div>
              <pre tabIndex={0} aria-label={`Contents of ${file.path}`} className="scroll-x max-h-[70dvh] whitespace-pre-wrap rounded-md border border-border bg-card p-3 font-mono text-xs [overflow-wrap:anywhere]">{pretty(file)}</pre>
            </>
          )}
        </Section>
      </div>
    </>
  );
}
