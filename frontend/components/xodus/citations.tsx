"use client";

import { useEffect, useState } from "react";
import { api, type Chunk } from "@/lib/api";
import { splitCitations } from "@/lib/citations";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { ErrorNote, Ticket } from "./parts";
import type { ApiError } from "@/lib/api";

/** Answer text with each [chunk:N] turned into a button that opens the cited passage. */
export function CitedText({ text, validIds }: { text: string; validIds?: number[] }) {
  const [open, setOpen] = useState<number | null>(null);
  const parts = splitCitations(text);
  return (
    <>
      <span className="prose-reply">
        {parts.map((p, i) => p.kind === "text" ? <span key={i}>{p.text}</span> : (
          <span key={i} className="whitespace-nowrap">
            {p.ids.map((id) => {
              const checked = !validIds || validIds.includes(id);
              return (
                <button key={id} type="button" onClick={() => setOpen(id)}
                  className="mx-0.5 rounded-sm border border-primary/40 bg-accent px-1 font-mono text-[0.78em] text-accent-foreground align-baseline hover:bg-primary hover:text-primary-foreground"
                  aria-label={`Open source passage ${id}${checked ? "" : " (not validated)"}`}>
                  {id}
                </button>
              );
            })}
          </span>
        ))}
      </span>
      <ChunkDialog id={open} onClose={() => setOpen(null)} />
    </>
  );
}

export function ChunkDialog({ id, onClose }: { id: number | null; onClose: () => void }) {
  const [chunk, setChunk] = useState<Chunk | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  useEffect(() => {
    if (id === null) return;
    let alive = true;
    setChunk(null);
    setError(null);
    api<Chunk>(`/api/knowledge/chunks/${id}`).then((c) => alive && setChunk(c), (e) => alive && setError(e));
    return () => { alive = false; };
  }, [id]);

  return (
    <Dialog open={id !== null} onOpenChange={(o) => { if (!o) onClose(); }}>
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Source passage {id}</DialogTitle>
          <DialogDescription>
            {chunk ? `${chunk.document}${chunk.locator ? `, ${chunk.locator}` : ""}${chunk.heading_path ? `, ${chunk.heading_path}` : ""}` : "Loading the passage the answer cites."}
          </DialogDescription>
        </DialogHeader>
        <ErrorNote error={error} title="This passage cannot be shown" hint="It may have been deleted since the answer was written." />
        {chunk && (
          <>
            <blockquote className="prose-reply border-l-2 border-primary pl-3 text-sm">{chunk.text}</blockquote>
            <Ticket fields={[["collection", chunk.collection], ["document", chunk.document], ["passage", `#${chunk.ordinal + 1}`],
              ["page", chunk.page ?? ""], ["chunk id", chunk.id]]} />
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
