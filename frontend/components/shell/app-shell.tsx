"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { Menu, Moon, Search, ShieldCheck, Sun } from "lucide-react";
import type { Health, OllamaEnvironment } from "@/lib/api";
import { SystemProvider, useSystem, type ResourceSummary } from "@/lib/system-context";
import { useTheme } from "@/lib/theme";
import { cn } from "@/lib/utils";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { Mark } from "@/components/xodus/mark";
import { CommandPalette } from "./command-palette";
import { VIEWS, isActive, type View } from "./views";

const GROUPS: View["group"][] = ["workspace", "your data", "agency", "system"];

function Brand() {
  return (
    <Link href="/" className="flex items-center gap-2.5 rounded-lg px-2 py-1">
      <Mark className="size-8 drop-shadow-[0_0_10px_rgb(var(--glow)/0.55)]" />
      <span className="leading-tight">
        <span className="block font-display text-[1.05rem] font-semibold tracking-[0.22em]">XODUS</span>
        <span className="block text-xs text-muted-foreground">your local AI workspace</span>
      </span>
    </Link>
  );
}

function Nav({ pending, onNavigate }: { pending: number; onNavigate?: () => void }) {
  const path = usePathname() ?? "/";
  return (
    <nav aria-label="Views" className="mt-6">
      {GROUPS.map((group) => (
        <div key={group} className="mb-3">
          {group !== "workspace" && <p className="eyebrow px-2.5 pb-1">{group}</p>}
          <ul className="space-y-0.5">
            {VIEWS.filter((v) => v.group === group).map((v) => {
              const active = isActive(path, v.href);
              const Icon = v.icon;
              return (
                <li key={v.href}>
                  <Link href={v.href} onClick={onNavigate} aria-current={active ? "page" : undefined}
                    className={cn("nav-link flex items-center gap-3 rounded-lg px-2.5 py-2 text-sm",
                      active ? "bg-sidebar-accent font-semibold text-foreground" : "text-foreground/80 hover:bg-sidebar-accent/70 hover:text-foreground")}>
                    <Icon aria-hidden="true" className={cn("size-4 shrink-0", active ? "text-primary drop-shadow-[0_0_6px_rgb(var(--glow)/0.8)]" : "text-muted-foreground")} />
                    <span className="flex-1">{v.label}</span>
                    {v.href === "/approvals/" && pending > 0 && (
                      <span className="rounded-full bg-warn-soft px-2 font-mono text-xs text-warn">{pending}<span className="sr-only"> waiting</span></span>
                    )}
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </nav>
  );
}

const OLLAMA_WORDS: Record<string, string> = {
  ok: "online", unreachable: "unreachable", model_missing: "model missing", remote_model: "remote model refused",
  digest_mismatch: "digest mismatch", error: "error",
};
const ENV_WORDS: Record<OllamaEnvironment["status"], string> = { ok: "checked", problem: "unsafe", unknown: "not verified" };

/** What the backend reports right now: Ollama, the active model and how Ollama was started (announced when it
 *  changes), then the latest measured memory (not announced: it changes every sample). */
function SystemCard({ className }: { className?: string }) {
  const { health, unreachable, resources } = useSystem();
  return (
    <section aria-label="System status" className={cn("glass rounded-xl p-3 text-sm", className)}>
      {unreachable ? (
        <p role="status" className="text-destructive">Backend <b>unreachable</b></p>
      ) : !health ? (
        <p role="status" className="text-muted-foreground">Checking the backend…</p>
      ) : (
        <HealthLines health={health} />
      )}
      {resources && !unreachable && <Meters r={resources} />}
    </section>
  );
}

function HealthLines({ health }: { health: Health }) {
  const ok = health.ollama.status === "ok";
  const env = health.ollama_environment;
  return (
    <div role="status" className="space-y-1">
      <p className="flex items-center justify-between gap-2">
        <span className="font-medium">Ollama</span>{" "}
        <span className={cn("flex items-center gap-1.5 text-xs font-medium", ok ? "text-ok" : "text-destructive")}>
          <span aria-hidden="true" className="size-1.5 rounded-full bg-current shadow-[0_0_6px_currentColor]" />
          {OLLAMA_WORDS[health.ollama.status] ?? health.ollama.status.replaceAll("_", " ")}
        </span>
      </p>
      <p className="truncate font-mono text-xs" title={health.ollama.model}>{health.ollama.model}</p>
      <p className="font-mono text-xs text-muted-foreground">
        profile <b className="text-foreground">{health.models.switching ? `switching to ${health.models.switching}` : health.models.active}</b>
        {" · "}ctx <b className="text-foreground">{health.models.num_ctx.toLocaleString()}</b>
      </p>
      <p title={env.message} className={cn("flex justify-between gap-2 text-xs", env.status === "problem" ? "text-destructive" : "text-muted-foreground")}>
        <span>Ollama settings</span>{" "}<b className={env.status === "problem" ? "text-destructive" : "text-foreground"}>{ENV_WORDS[env.status]}</b>
      </p>
    </div>
  );
}

function Meters({ r }: { r: ResourceSummary }) {
  const check = (metric: string) => r.thresholds.checks.find((c) => c.metric === metric);
  const uss = check("xodus_uss");
  const vram = check("device_vram");
  const free = check("system_available");
  const gib = (mib: number) => (mib / 1024).toFixed(1);
  return (
    <div className="mt-3 space-y-2 border-t border-border pt-3 text-xs">
      <Meter label="XODUS memory" value={uss?.value_gb != null ? `${uss.value_gb.toFixed(2)} GB` : "n/a"}
        fraction={uss?.value_gb != null ? uss.value_gb / uss.stop_gb : null} level={uss?.level} />
      <Meter label="GPU memory (all apps)" value={r.device_vram ? `${gib(r.device_vram.used)} / ${gib(r.device_vram.total)} GiB` : "n/a"}
        fraction={r.device_vram ? r.device_vram.used / r.device_vram.total : null} level={vram?.level} />
      <Meter label="RAM available" value={free?.value_gb != null ? `${free.value_gb.toFixed(1)} GB` : "n/a"} fraction={null} level={free?.level} />
      <p className="text-muted-foreground">Measured by the backend every 15 s.</p>
    </div>
  );
}

function Meter({ label, value, fraction, level }: { label: string; value: string; fraction: number | null; level?: string }) {
  return (
    <div>
      <p className="flex justify-between gap-2"><span className="text-muted-foreground">{label}</span>{" "}<span className="font-mono">{value}</span></p>
      {fraction !== null && (
        <span aria-hidden="true" className="mt-1 block h-1 overflow-hidden rounded-full bg-muted">
          <span className={cn("block h-1 rounded-full transition-[width] duration-700",
            level === "stop" ? "bg-destructive" : level === "warn" ? "bg-warn" : "bg-primary shadow-[0_0_8px_rgb(var(--glow)/0.7)]")}
            style={{ width: `${Math.max(2, Math.min(100, fraction * 100))}%` }} />
        </span>
      )}
    </div>
  );
}

/** Settings or models that would let data leave this computer. Shown until the backend reports them fixed. */
function PrivacyAlert({ health }: { health: Health }) {
  const env = health.ollama_environment;
  const failed = env.status === "problem" ? env.checks.filter((c) => c.ok === false) : [];
  const remote = [health.ollama, health.knowledge.embedding].filter((s) => s.status === "remote_model");
  if (!failed.length && !remote.length) return null;
  return (
    <div role="alert" className="enter rounded-xl border border-destructive/40 border-l-2 border-l-destructive bg-destructive-soft px-4 py-2.5 text-sm text-destructive">
      {failed.length > 0 && <p className="font-medium">{env.message}</p>}
      <ul className="list-disc pl-5">
        {failed.map((c) => <li key={c.id}>{c.detail}</li>)}
        {remote.map((s, i) => <li key={`remote-${i}`}>{s.message}</li>)}
      </ul>
      {failed.length > 0 && env.fix && <p className="text-foreground">{env.fix}</p>}
    </div>
  );
}

function ThemeToggle() {
  const { dark, set } = useTheme();
  return (
    <Button variant="ghost" size="icon" aria-label={dark ? "Switch to light theme" : "Switch to dark theme"} onClick={() => set(dark ? "light" : "dark")}>
      {dark ? <Sun /> : <Moon />}
    </Button>
  );
}

/** Below the sidebar breakpoint the status card is in the menu; this pill says whether anything needs attention. */
function StatusPill() {
  const { health, unreachable } = useSystem();
  const [text, bad] = unreachable ? ["backend unreachable", true]
    : !health ? ["checking…", false]
    : health.ollama.status !== "ok" ? [`ollama ${OLLAMA_WORDS[health.ollama.status] ?? health.ollama.status}`, true]
    : health.ollama_environment.status === "problem" ? ["unsafe Ollama settings", true]
    : ["local · ready", false];
  return (
    <span className={cn("hidden items-center gap-1.5 rounded-full border px-2.5 py-1 font-mono text-xs sm:flex lg:hidden",
      bad ? "border-destructive/40 text-destructive" : "border-border text-muted-foreground")}>
      <span aria-hidden="true" className={cn("size-1.5 rounded-full", bad ? "bg-destructive" : "bg-ok shadow-[0_0_6px_currentColor]")} />
      {text}
    </span>
  );
}

function Shell({ children }: { children: ReactNode }) {
  const [menu, setMenu] = useState(false);
  const [palette, setPalette] = useState(false);
  const { health, unreachable, pending } = useSystem();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPalette((open) => !open);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="min-h-dvh lg:grid lg:grid-cols-[16rem_1fr]">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded-md focus:bg-card focus:px-3 focus:py-2">
        Skip to content
      </a>
      <aside className="hidden border-r border-sidebar-border bg-sidebar/85 lg:sticky lg:top-0 lg:flex lg:h-dvh lg:flex-col lg:justify-between lg:overflow-y-auto lg:p-4">
        <div>
          <Brand />
          <Nav pending={pending} />
        </div>
        <SystemCard className="mt-4" />
      </aside>

      <div className="min-w-0">
        <div className="sticky top-0 z-30 border-b border-border/70 bg-background/75 backdrop-blur-xl">
          <div className="flex h-14 items-center gap-2 px-4 sm:px-6">
            <Link href="/" className="flex items-center gap-2 lg:hidden">
              <Mark className="size-7" />
              <span className="font-display text-base font-semibold tracking-[0.2em]">XODUS</span>
            </Link>
            <button type="button" onClick={() => setPalette(true)} aria-keyshortcuts="Control+K"
              className="ml-auto flex h-9 items-center gap-2 rounded-lg border border-border bg-card/70 px-2.5 text-sm text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground sm:w-80 sm:px-3 lg:ml-0">
              <Search className="size-4 shrink-0" aria-hidden="true" />
              <span className="sr-only sm:not-sr-only sm:flex-1 sm:text-left">Search or run a command</span>
              <span className="kbd hidden sm:inline">Ctrl K</span>
            </button>
            <div className="flex items-center gap-1.5 lg:ml-auto">
              <StatusPill />
              <ThemeToggle />
              <Sheet open={menu} onOpenChange={setMenu}>
                <SheetTrigger asChild>
                  <Button variant="outline" size="icon" className="lg:hidden" aria-label="Open the list of views"><Menu /></Button>
                </SheetTrigger>
                <SheetContent side="left" className="w-[18rem] overflow-y-auto bg-sidebar p-4">
                  <SheetHeader className="sr-only p-0">
                    <SheetTitle>XODUS</SheetTitle>
                    <SheetDescription>Views</SheetDescription>
                  </SheetHeader>
                  <Brand />
                  <Nav pending={pending} onNavigate={() => setMenu(false)} />
                  <SystemCard className="mt-2" />
                </SheetContent>
              </Sheet>
            </div>
          </div>
        </div>

        <div className="space-y-2 px-4 pt-3 empty:hidden sm:px-8">
          {pending > 0 && (
            <div role="status" className="enter flex flex-wrap items-center gap-x-2 gap-y-1 rounded-xl border border-warn/40 bg-warn-soft px-4 py-2 text-sm shadow-[0_0_28px_-14px_rgb(var(--warn-rgb)/0.9)]">
              <ShieldCheck className="size-4 text-warn" aria-hidden="true" />
              <Link href="/approvals/" className="font-medium text-warn underline underline-offset-2">
                {pending === 1 ? "1 action is waiting for your approval." : `${pending} actions are waiting for your approval.`}
              </Link>
              <span className="text-foreground">The task is paused until you decide.</span>
            </div>
          )}
          {unreachable && (
            <div role="alert" className="enter rounded-xl border border-destructive/40 border-l-2 border-l-destructive bg-destructive-soft px-4 py-2 text-sm text-destructive">
              The XODUS backend is not answering. Start it with <code>uv run python -m xodus</code> in <code>backend/</code>; this page retries every 5 seconds.
            </div>
          )}
          {health && !unreachable && <PrivacyAlert health={health} />}
        </div>
        <main id="main" className="mx-auto w-full max-w-[80rem] px-4 py-6 sm:px-8 sm:py-8">{children}</main>
      </div>
      <CommandPalette open={palette} onOpenChange={setPalette} />
    </div>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <SystemProvider>
      <Shell>{children}</Shell>
    </SystemProvider>
  );
}
