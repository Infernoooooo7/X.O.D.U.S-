"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState, type ReactNode } from "react";
import { Menu } from "lucide-react";
import { api, type Approval, type Health } from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { cn } from "@/lib/utils";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";

export const VIEWS = [
  { href: "/", label: "Workspace", hint: "Chat, ask your documents, run tasks" },
  { href: "/history/", label: "History", hint: "Saved conversations" },
  { href: "/knowledge/", label: "Knowledge", hint: "Collections, files, ingestion" },
  { href: "/memory/", label: "Memory", hint: "Notes, facts, preferences" },
  { href: "/capabilities/", label: "Capabilities", hint: "What XODUS can do, and with which rights" },
  { href: "/approvals/", label: "Approvals", hint: "Actions waiting for you" },
  { href: "/activity/", label: "Activity", hint: "Tasks and the audit log" },
  { href: "/models/", label: "Models", hint: "Profiles, footprint, switching" },
  { href: "/resources/", label: "Resources", hint: "Processes, memory, VRAM" },
  { href: "/experiments/", label: "Experiments", hint: "Measurement results" },
  { href: "/settings/", label: "Settings", hint: "Configuration and theme" },
] as const;

function isActive(path: string, href: string) {
  return href === "/" ? path === "/" : path.startsWith(href.replace(/\/$/, ""));
}

function Nav({ pending, onNavigate }: { pending: number; onNavigate?: () => void }) {
  const path = usePathname() ?? "/";
  return (
    <nav aria-label="Views">
      <ol className="space-y-0.5">
        {VIEWS.map((v, i) => {
          const active = isActive(path, v.href);
          return (
            <li key={v.href}>
              <Link href={v.href} onClick={onNavigate} aria-current={active ? "page" : undefined}
                className={cn("group flex items-baseline gap-3 rounded-sm px-2 py-1.5 text-sm transition-colors",
                  active ? "bg-sidebar-accent font-semibold text-foreground" : "text-foreground/85 hover:bg-sidebar-accent/60")}>
                <span className={cn("card-number w-5 shrink-0", active && "text-primary")}>{String(i + 1).padStart(2, "0")}</span>
                <span className="flex-1">{v.label}</span>
                {v.label === "Approvals" && pending > 0 && (
                  <span className="rounded-sm bg-warn-soft px-1.5 font-mono text-xs text-warn" aria-label={`${pending} waiting`}>{pending}</span>
                )}
              </Link>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

function HealthStrip({ health, unreachable }: { health: Health | null; unreachable: boolean }) {
  if (unreachable) {
    return <p role="status" className="ticket border-destructive/50 text-destructive">backend <b className="text-destructive">unreachable</b></p>;
  }
  if (!health) return <p role="status" className="ticket">checking the backend…</p>;
  const ok = health.ollama.status === "ok";
  return (
    <p role="status" aria-live="polite" className="ticket min-w-0 flex-col !gap-0 [&>span]:block [&>span]:max-w-full [&>span]:truncate">
      <span>backend <b>{health.backend.status}</b></span>
      <span className={ok ? "" : "text-destructive"}>ollama <b className={ok ? "" : "text-destructive"}>{health.ollama.status.replace("_", " ")}</b></span>
      <span title={health.ollama.model}>model <b>{health.ollama.model}</b></span>
      <span>profile <b>{health.models.switching ? `switching to ${health.models.switching}` : health.models.active}</b> ctx <b>{health.models.num_ctx}</b></span>
      <span>loopback <b>127.0.0.1</b></span>
    </p>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const health = usePoll(() => api<Health>("/api/health"), 5000);
  const approvals = usePoll(() => api<Approval[]>("/api/approvals"), 3000);
  const pending = approvals.data?.length ?? 0;
  const unreachable = health.error?.code === "backend_unreachable";

  return (
    <div className="min-h-dvh lg:grid lg:grid-cols-[15.5rem_1fr]">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded-sm focus:bg-card focus:px-3 focus:py-2">
        Skip to content
      </a>
      <aside className="hidden border-r border-sidebar-border bg-sidebar lg:sticky lg:top-0 lg:flex lg:h-dvh lg:flex-col lg:justify-between lg:p-4">
        <div>
          <Link href="/" className="mb-5 block px-2">
            <span className="font-display text-xl font-bold tracking-[0.18em]">XODUS</span>
            <span className="eyebrow block">local workspace</span>
          </Link>
          <Nav pending={pending} />
        </div>
        <HealthStrip health={health.data} unreachable={unreachable} />
      </aside>

      <div className="min-w-0">
        <div className="sticky top-0 z-30 flex items-center justify-between gap-2 border-b border-border bg-background/95 px-4 py-2 backdrop-blur lg:hidden">
          <Link href="/" className="font-display text-lg font-bold tracking-[0.18em]">XODUS</Link>
          <div className="flex items-center gap-2">
            {pending > 0 && (
              <Link href="/approvals/" className="rounded-sm bg-warn-soft px-2 py-1 font-mono text-xs text-warn">{pending} waiting</Link>
            )}
            <Sheet open={open} onOpenChange={setOpen}>
              <SheetTrigger asChild>
                <Button variant="outline" size="icon" aria-label="Open the list of views"><Menu /></Button>
              </SheetTrigger>
              <SheetContent side="left" className="w-[18rem] bg-sidebar p-4">
                <SheetHeader className="p-0">
                  <SheetTitle className="font-display tracking-[0.18em]">XODUS</SheetTitle>
                  <SheetDescription>Views</SheetDescription>
                </SheetHeader>
                <Nav pending={pending} onNavigate={() => setOpen(false)} />
                <HealthStrip health={health.data} unreachable={unreachable} />
              </SheetContent>
            </Sheet>
          </div>
        </div>

        {pending > 0 && (
          <div role="status" className="border-b border-warn/40 bg-warn-soft px-4 py-2 text-sm sm:px-8">
            <Link href="/approvals/" className="font-medium text-warn underline underline-offset-2">
              {pending === 1 ? "1 action is waiting for your approval." : `${pending} actions are waiting for your approval.`}
            </Link>{" "}
            <span className="text-foreground">The task is paused until you decide.</span>
          </div>
        )}
        {unreachable && (
          <div role="alert" className="border-b border-destructive/40 bg-destructive-soft px-4 py-2 text-sm text-destructive sm:px-8">
            The XODUS backend is not answering. Start it with <code>uv run python -m xodus</code> in <code>backend/</code>; this page retries every 5 seconds.
          </div>
        )}
        <main id="main" className="mx-auto w-full max-w-[76rem] px-4 py-6 sm:px-8 sm:py-8">{children}</main>
      </div>
    </div>
  );
}
