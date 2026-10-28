"use client";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import {
  CornerDownLeft, FileSearch, ListChecks, MessageSquare, Monitor, Moon, PauseCircle, PlayCircle, Plus, ScrollText, Search,
  ShieldCheck, Sun, type LucideIcon,
} from "lucide-react";
import { api, post, type ApiError } from "@/lib/api";
import { requestCompose, type ComposeRequest } from "@/lib/compose";
import { score } from "@/lib/palette-search";
import { useSystem } from "@/lib/system-context";
import { useTheme } from "@/lib/theme";
import { cn } from "@/lib/utils";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { VIEWS } from "./views";

type Outcome = { ok: boolean; text: string };
type Command = {
  id: string; group: "Go to" | "Workspace" | "System"; label: string; hint?: string; keywords?: string; icon: LucideIcon;
  /** Navigation returns nothing (the palette closes); an action returns what happened, shown in the palette. */
  run: () => void | Promise<Outcome>;
};

const GROUPS: Command["group"][] = ["Workspace", "Go to", "System"];

/** Matching commands grouped for display: groups ordered by their best match, items by score (stable otherwise).
 *  The flat list, in exactly the displayed order, is what the arrow keys walk through. */
function arrange(commands: Command[], query: string): { group: Command["group"]; items: Command[] }[] {
  const scored = commands.map((c) => ({ c, s: score(c, query) })).filter((x): x is { c: Command; s: number } => x.s !== null);
  return GROUPS.map((group) => ({ group, entries: scored.filter((x) => x.c.group === group).sort((a, b) => a.s - b.s) }))
    .filter((g) => g.entries.length)
    .sort((a, b) => a.entries[0].s - b.entries[0].s || GROUPS.indexOf(a.group) - GROUPS.indexOf(b.group))
    .map((g) => ({ group: g.group, items: g.entries.map((x) => x.c) }));
}

/** Ctrl+K: every view and a few real actions, searchable and run from the keyboard. Nothing here is simulated. */
export function CommandPalette({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const router = useRouter();
  const pathname = usePathname() ?? "/";
  const { health, pending, refreshHealth } = useSystem();
  const { dark, choice, set: setThemeChoice } = useTheme();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const listId = useId();
  const options = useRef(new Map<string, HTMLDivElement>());
  // Work that moves focus (into the message box) must wait until the dialog has closed and released its focus trap.
  const afterClose = useRef<(() => void) | null>(null);

  useEffect(() => {
    if (open) { setQuery(""); setActive(0); setOutcome(null); }
  }, [open]);

  const paused = health?.knowledge.ingestion.paused;
  const commands = useMemo<Command[]>(() => {
    const compose = (detail: ComposeRequest) => () => {
      afterClose.current = () => {
        if (pathname === "/") requestCompose(detail);
        else router.push(detail.fresh || !detail.mode ? "/" : `/?mode=${detail.mode}`);
      };
    };
    const list: Command[] = [
      { id: "new", group: "Workspace", label: "New conversation", hint: "Start an empty conversation", icon: Plus, run: compose({ fresh: true }) },
      { id: "chat", group: "Workspace", label: "Chat with the local model", hint: "Earlier turns are sent with each message", icon: MessageSquare, run: compose({ mode: "chat" }) },
      { id: "ask", group: "Workspace", label: "Ask your documents", hint: "Answers only from ingested documents, with checked citations", keywords: "knowledge rag search", icon: FileSearch, run: compose({ mode: "ask" }) },
      { id: "task", group: "Workspace", label: "Run a task", hint: "Capabilities step by step; changes wait for your approval", keywords: "agent tools", icon: ListChecks, run: compose({ mode: "task" }) },
    ];
    if (pending > 0) {
      list.push({ id: "approvals", group: "Workspace", label: `Review ${pending} waiting approval${pending === 1 ? "" : "s"}`, hint: "A task is paused until you decide", icon: ShieldCheck, run: () => router.push("/approvals/") });
    }
    for (const v of VIEWS) {
      list.push({ id: `go${v.href}`, group: "Go to", label: v.label, hint: v.hint, keywords: "open view page", icon: v.icon, run: () => router.push(v.href) });
    }
    list.push(dark
      ? { id: "light", group: "System", label: "Switch to light theme", keywords: "theme appearance", icon: Sun, run: () => setThemeChoice("light") }
      : { id: "dark", group: "System", label: "Switch to dark theme", keywords: "theme appearance", icon: Moon, run: () => setThemeChoice("dark") });
    if (choice !== "system") {
      list.push({ id: "system-theme", group: "System", label: "Follow the Windows theme", keywords: "theme appearance", icon: Monitor, run: () => setThemeChoice("system") });
    }
    if (paused !== undefined) {
      list.push(paused
        ? { id: "resume", group: "System", label: "Resume background ingestion", keywords: "knowledge worker", icon: PlayCircle,
            run: async () => { await post("/api/ingestion/resume"); await refreshHealth(); return { ok: true, text: "Background ingestion resumed." }; } }
        : { id: "pause", group: "System", label: "Pause background ingestion", keywords: "knowledge worker", icon: PauseCircle,
            run: async () => { await post("/api/ingestion/pause"); await refreshHealth(); return { ok: true, text: "Background ingestion paused. A running file resumes from its last saved batch." }; } });
    }
    list.push({ id: "verify", group: "System", label: "Verify the audit log", hint: "Recompute the hash chain of every record", keywords: "security integrity", icon: ScrollText,
      run: async () => {
        const r = await api<{ ok: boolean; records: number; first_bad_seq: number | null; reason: string | null }>("/api/audit/verify");
        return r.ok ? { ok: true, text: `The audit log is intact: ${r.records.toLocaleString()} record(s) checked.` }
          : { ok: false, text: `The audit log check failed at record ${r.first_bad_seq}: ${r.reason}` };
      } });
    return list;
  }, [pathname, router, pending, dark, choice, setThemeChoice, paused, refreshHealth]);

  const arranged = arrange(commands, query);
  const shown = arranged.flatMap((g) => g.items);
  const current = shown[Math.min(active, shown.length - 1)];
  const optionId = (c: Command) => `${listId}-${c.id}`;

  useEffect(() => {
    if (current) options.current.get(current.id)?.scrollIntoView({ block: "nearest" });
  }, [current]);

  async function execute(c: Command) {
    if (busy) return;
    setBusy(true);
    setOutcome(null);
    try {
      const result = await c.run();
      if (result) setOutcome(result);
      else onOpenChange(false);
    } catch (err) {
      const e = err as ApiError;
      setOutcome({ ok: false, text: `${e.message ?? String(err)} (${e.code ?? "error"})` });
    } finally {
      setBusy(false);
    }
  }

  // On the dialog, so the keys work from the search box and from the list (which Tab can reach, to scroll it).
  function onKeys(e: React.KeyboardEvent) {
    if (!shown.length) return;
    if (e.key === "ArrowDown") { e.preventDefault(); setActive((i) => (Math.min(i, shown.length - 1) + 1) % shown.length); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive((i) => (Math.min(i, shown.length - 1) - 1 + shown.length) % shown.length); }
    else if (e.key === "Enter" && current) { e.preventDefault(); void execute(current); }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent showCloseButton={false} aria-describedby={undefined} onKeyDown={onKeys}
        onCloseAutoFocus={(e) => {
          const next = afterClose.current;
          if (!next) return; // closed with Escape or after an action: focus goes back where it was
          e.preventDefault();
          afterClose.current = null;
          next();
        }}
        className="glass top-[12%] translate-y-0 gap-0 overflow-hidden rounded-2xl p-0 sm:max-w-xl">
        <DialogTitle className="sr-only">Command palette</DialogTitle>
        <div className="flex items-center gap-2 border-b border-border px-3.5">
          <Search className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          <input autoFocus role="combobox" aria-expanded="true" aria-controls={listId} aria-autocomplete="list"
            aria-activedescendant={current ? optionId(current) : undefined} aria-label="Search views and actions"
            placeholder="Search views and actions…" value={query}
            onChange={(e) => { setQuery(e.target.value); setActive(0); }}
            className="h-12 min-w-0 flex-1 bg-transparent text-[0.95rem] outline-none placeholder:text-muted-foreground" />
          <span className="kbd">Esc</span>
        </div>
        <div id={listId} role="listbox" aria-label="Views and actions" tabIndex={0}
          className="max-h-[min(58vh,26rem)] overflow-y-auto p-2 outline-none focus-visible:ring-2 focus-visible:ring-ring/60">

          {arranged.map(({ group, items }) => {
            return (
              <div key={group} role="group" aria-label={group} className="mb-1">
                <p aria-hidden="true" className="eyebrow px-2.5 pb-1 pt-2">{group.toLowerCase()}</p>
                {items.map((c) => {
                  const on = c === current;
                  const Icon = c.icon;
                  return (
                    <div key={c.id} id={optionId(c)} role="option" aria-selected={on}
                      ref={(el) => { if (el) options.current.set(c.id, el); else options.current.delete(c.id); }}
                      onMouseMove={() => setActive(shown.indexOf(c))} onClick={() => void execute(c)}
                      className={cn("flex cursor-pointer items-center gap-3 rounded-lg px-2.5 py-2 transition-colors",
                        on ? "bg-accent text-accent-foreground shadow-[inset_0_0_0_1px_rgb(var(--glow)/0.25)]" : "text-foreground")}>
                      <span className={cn("grid size-8 shrink-0 place-items-center rounded-md border", on ? "border-primary/40 text-primary" : "border-border text-muted-foreground")}>
                        <Icon className="size-4" aria-hidden="true" />
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-sm font-medium">{c.label}</span>
                        {c.hint && <span className="block truncate text-xs text-muted-foreground">{c.hint}</span>}
                      </span>
                      {on && <CornerDownLeft className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />}
                    </div>
                  );
                })}
              </div>
            );
          })}
          {shown.length === 0 && <p className="px-3 py-8 text-center text-sm text-muted-foreground">Nothing matches “{query}”.</p>}
        </div>
        <div className="flex items-center justify-between gap-3 border-t border-border px-3.5 py-2 text-xs text-muted-foreground">
          <p role="status" className={cn("min-w-0", outcome && !outcome.ok && "text-destructive")}>
            {busy ? "Working…" : outcome?.text ?? ""}
          </p>
          <span className="hidden shrink-0 sm:inline"><span className="kbd">↑</span> <span className="kbd">↓</span> move · <span className="kbd">Enter</span> run</span>
        </div>
      </DialogContent>
    </Dialog>
  );
}
