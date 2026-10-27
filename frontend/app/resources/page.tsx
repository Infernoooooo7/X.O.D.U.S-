"use client";

import { api, post } from "@/lib/api";
import { mib } from "@/lib/format";
import { useAction, usePoll } from "@/lib/hooks";
import type { Tone } from "@/lib/status";
import { Button } from "@/components/ui/button";
import { Confirm } from "@/components/xodus/confirm";
import { ErrorNote, Note, PageHeader, Section, Status, Tag, Ticket } from "@/components/xodus/parts";

type Check = { metric: string; label: string; value_gb: number | null; warn_gb: number | null; stop_gb: number; level: string; note: string | null };
type Line = { line: string; label: string; processes: number | null; included: boolean; uss_mib: number | null; working_set_mib: number | null;
  private_commit_mib: number | null; note: string | null };
type Proc = { pid: number; name: string; group: string; tier: string; role: string; owner: string | null; supervised: boolean;
  uss_mib: number | null; working_set_mib: number | null; private_commit_mib: number | null; gpu_dedicated_mib: number | null;
  gpu_shared_mib: number | null; errors: string[] };
type View = {
  totals: { uss_mib: number; working_set_mib: number; private_commit_mib: number; uss_complete: boolean; included: string; excluded: string };
  lines: Line[]; processes: Proc[]; device_vram: { total: number; used: number; free: number } | null;
  gpu_ollama: { dedicated_mib: number | null; shared_mib: number | null };
  measurement: { nvml: string; gpu_counters: string; sample_ms: number };
  thresholds: { level: string; checks: Check[]; enforced: boolean; enforcement_note: string };
  ingestion: { paused: boolean; pending_jobs: number; worker: { running: boolean; pid: number | null } };
  supervised: { pid: number; tier: string; role: string; owner: string; idle_s: number; idle_timeout_s: number | null; memory_limit_bytes: number | null }[];
  limits: { max_optional_processes: number; optional_idle_timeout_s: number; optional_memory_limit_mib: number | null };
};

const LEVEL: Record<string, { label: string; tone: Tone }> = {
  ok: { label: "Below the warning level", tone: "good" },
  warn: { label: "Warning level reached", tone: "wait" },
  stop: { label: "Stop level reached", tone: "bad" },
  unknown: { label: "Could not be measured", tone: "neutral" },
};

export default function Resources() {
  const view = usePoll(() => api<View>("/api/resources"), 2000);
  const action = useAction();
  const v = view.data;
  const level = LEVEL[v?.thresholds.level ?? "unknown"];

  return (
    <>
      <PageHeader number="09" title="Resources" lead="Every process XODUS runs or depends on, with its tier and memory, against the thresholds in the configuration. Updated every 2 seconds." />
      <ErrorNote error={view.error} title="Measurements cannot be loaded" hint={v ? "Showing the last values received." : undefined} />
      {v && (
        <>
          <section aria-labelledby="levels" className="panel p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 id="levels" className="text-lg font-semibold">Thresholds</h2>
              <span role="status"><Status tone={level.tone}>{level.label}</Status></span>
            </div>
            <ul className="mt-3 grid gap-3 md:grid-cols-3">
              {v.thresholds.checks.map((c) => <Meter key={c.metric} c={c} />)}
            </ul>
            <p className="mt-3 text-sm text-muted-foreground">{v.thresholds.enforcement_note}</p>
          </section>

          <Section title="Memory by line" id="lines" description={`Totals include the ${v.totals.included}. Excluded: ${v.totals.excluded}.`}>
            <ul className="space-y-2 sm:hidden">
              {v.lines.map((l) => (
                <li key={l.line} className={`panel p-3 text-sm ${l.included ? "" : "text-muted-foreground"}`}>
                  <p className="font-medium">{l.label}{!l.included && <> <Tag>not in the total</Tag></>}</p>
                  {l.note && <p className="text-xs text-muted-foreground">{l.note}</p>}
                  <Ticket fields={[["processes", l.processes ?? ""], ["USS", l.uss_mib !== null ? mib(l.uss_mib) : ""],
                    ["working set", l.working_set_mib !== null ? mib(l.working_set_mib) : ""], ["private commit", l.private_commit_mib !== null ? mib(l.private_commit_mib) : ""]]} />
                </li>
              ))}
              <li className="panel p-3 text-sm">
                <p className="font-semibold">XODUS-related total</p>
                <Ticket fields={[["USS", `${mib(v.totals.uss_mib)}${v.totals.uss_complete ? "" : " (incomplete)"}`],
                  ["working set", mib(v.totals.working_set_mib)], ["private commit", mib(v.totals.private_commit_mib)]]} />
              </li>
            </ul>
            <div className="hidden panel sm:block">
              <table className="w-full text-sm">
                <caption className="sr-only">Memory per measurement line</caption>
                <thead className="text-left font-mono text-xs text-muted-foreground">
                  <tr><th className="p-2 font-normal">Line</th><th className="p-2 text-right font-normal">Processes</th>
                    <th className="p-2 text-right font-normal">USS</th><th className="p-2 text-right font-normal">Working set</th>
                    <th className="p-2 text-right font-normal">Private commit</th></tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {v.lines.map((l) => (
                    <tr key={l.line} className={l.included ? "" : "text-muted-foreground"}>
                      <td className="p-2">{l.label}{!l.included && <Tag>not in the total</Tag>}{l.note && <span className="block text-xs text-muted-foreground">{l.note}</span>}</td>
                      <td className="p-2 text-right font-mono">{l.processes ?? ""}</td>
                      <td className="p-2 text-right font-mono">{l.uss_mib !== null ? mib(l.uss_mib) : ""}</td>
                      <td className="p-2 text-right font-mono">{l.working_set_mib !== null ? mib(l.working_set_mib) : ""}</td>
                      <td className="p-2 text-right font-mono">{l.private_commit_mib !== null ? mib(l.private_commit_mib) : ""}</td>
                    </tr>
                  ))}
                  <tr className="font-semibold">
                    <td className="p-2">XODUS-related total</td><td className="p-2" />
                    <td className="p-2 text-right font-mono">{mib(v.totals.uss_mib)}{!v.totals.uss_complete && " (incomplete)"}</td>
                    <td className="p-2 text-right font-mono">{mib(v.totals.working_set_mib)}</td>
                    <td className="p-2 text-right font-mono">{mib(v.totals.private_commit_mib)}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p className="mt-2 text-xs text-muted-foreground">USS is memory only that process uses (freed when it exits). Working sets double-count shared pages; private commit is reserved memory, not physical RAM.</p>
            <Ticket fields={[["device VRAM", v.device_vram ? `${mib(v.device_vram.used)} of ${mib(v.device_vram.total)} (all apps)` : "unavailable"],
              ["Ollama GPU dedicated", v.gpu_ollama.dedicated_mib !== null ? mib(v.gpu_ollama.dedicated_mib) : "n/a"],
              ["Ollama GPU shared (spill to RAM)", v.gpu_ollama.shared_mib !== null ? mib(v.gpu_ollama.shared_mib) : "n/a"],
              ["NVML", v.measurement.nvml], ["GPU counters", v.measurement.gpu_counters], ["sample took", `${v.measurement.sample_ms} ms`]]} />
          </Section>

          <Section title="Background ingestion" id="ingestion">
            <div className="flex flex-wrap items-center gap-3 panel p-4">
              <Status tone={v.ingestion.paused ? "wait" : v.ingestion.worker.running ? "active" : "neutral"}>
                {v.ingestion.paused ? "Paused" : v.ingestion.worker.running ? `Worker running, pid ${v.ingestion.worker.pid}` : "Idle"}
              </Status>
              <span className="text-sm text-muted-foreground">{v.ingestion.pending_jobs} job(s) queued or running</span>
              {v.ingestion.paused ? (
                <Button size="sm" disabled={action.busy} onClick={() => action.run(async () => { await post("/api/ingestion/resume"); await view.refresh(); })}>Resume ingestion</Button>
              ) : (
                <Button size="sm" variant="outline" disabled={action.busy} onClick={() => action.run(async () => { await post("/api/ingestion/pause"); await view.refresh(); })}>Pause ingestion</Button>
              )}
              <p className="w-full text-xs text-muted-foreground">Pausing stops the worker at once; its file goes back to the queue and continues from the last saved batch. A pause lasts until you resume, also across restarts.</p>
            </div>
          </Section>

          <Section title="Processes" id="processes" description={`Optional processes: at most ${v.limits.max_optional_processes} at once, stopped after ${Math.round(v.limits.optional_idle_timeout_s / 60)} min idle${v.limits.optional_memory_limit_mib ? `, memory limit ${mib(v.limits.optional_memory_limit_mib)} each` : ""}.`}>
            <ErrorNote error={action.error} title="That did not work" />
            <ul className="grid gap-2 md:grid-cols-2">
              {v.processes.map((p) => {
                const sup = v.supervised.find((s) => s.pid === p.pid);
                return (
                  <li key={p.pid} className="panel p-3 text-sm">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">{p.name}</span>
                      <Tag>tier {p.tier}</Tag>
                      <Tag>{p.role}{p.owner && p.owner !== p.role ? `: ${p.owner}` : ""}</Tag>
                      {p.supervised && <Tag>launched by XODUS</Tag>}
                    </div>
                    <Ticket fields={[["pid", p.pid], ["USS", p.uss_mib !== null ? mib(p.uss_mib, 1) : "n/a"], ["working set", mib(p.working_set_mib, 1)],
                      ["private commit", mib(p.private_commit_mib, 1)], ["GPU dedicated", p.gpu_dedicated_mib !== null ? mib(p.gpu_dedicated_mib) : ""],
                      ["GPU shared", p.gpu_shared_mib !== null ? mib(p.gpu_shared_mib) : ""], ["idle", sup ? `${Math.round(sup.idle_s)} s` : ""]]} />
                    {p.errors.length > 0 && <p className="mt-1 text-xs text-destructive">{p.errors.join("; ")}</p>}
                    {sup && sup.tier === "optional" && (
                      <Confirm title={`Stop ${p.owner ?? p.name}?`} confirmLabel="Stop process"
                        description="Its whole process group is ended. It starts again the next time its capability is used."
                        onConfirm={() => void action.run(async () => { await post(`/api/processes/${p.pid}/stop`); await view.refresh(); })}
                        trigger={<Button size="sm" variant="outline" className="mt-2">Stop process</Button>} />
                    )}
                  </li>
                );
              })}
            </ul>
          </Section>
        </>
      )}
      {!v && !view.error && <Note>Measuring…</Note>}
    </>
  );
}

/** A 270-degree gauge of one threshold check: the measured value against its warn (amber) and stop (coral) ticks. The
 *  scale ends 15% above the stop level, as the old bar did, so a full arc means "far past stop" or, for available
 *  memory, "far above the floor". */
function Meter({ c }: { c: Check }) {
  const st = LEVEL[c.level] ?? LEVEL.unknown;
  const max = c.stop_gb * 1.15;
  const fraction = c.value_gb !== null ? Math.min(1, c.value_gb / max) : 0;
  const below = c.metric === "system_available";
  const R = 46, C = 2 * Math.PI * R;
  const dash = (f: number) => `${C * 0.75 * f} ${C}`;
  const tick = (gb: number) => {
    const a = ((135 + 270 * Math.min(1, gb / max)) * Math.PI) / 180;
    return { x1: 60 + Math.cos(a) * 37, y1: 60 + Math.sin(a) * 37, x2: 60 + Math.cos(a) * 55, y2: 60 + Math.sin(a) * 55 };
  };
  const color = c.level === "stop" ? "var(--destructive)" : c.level === "warn" ? "var(--warn)" : "var(--primary)";
  return (
    <li className="rounded-xl border border-border bg-background/40 p-3">
      <p className="text-sm font-medium">{c.label}</p>
      <div className="relative mx-auto mt-2 size-36">
        <svg viewBox="0 0 120 120" className="size-full" role="img"
          aria-label={`${c.value_gb ?? "unknown"} GB; ${below ? `stop below ${c.stop_gb} GB` : `warn at ${c.warn_gb} GB, stop at ${c.stop_gb} GB`}`}>
          <circle cx="60" cy="60" r={R} fill="none" stroke="var(--muted)" strokeWidth="8" strokeLinecap="round" strokeDasharray={dash(1)} transform="rotate(135 60 60)" />
          {c.value_gb !== null && (
            <circle cx="60" cy="60" r={R} fill="none" stroke={color} strokeWidth="8" strokeLinecap="round" strokeDasharray={dash(fraction)}
              transform="rotate(135 60 60)" style={{ filter: `drop-shadow(0 0 5px ${color})`, transition: "stroke-dasharray 700ms ease" }} />
          )}
          {c.warn_gb !== null && <line {...tick(c.warn_gb)} stroke="var(--warn)" strokeWidth="2.5" strokeLinecap="round" />}
          <line {...tick(c.stop_gb)} stroke="var(--destructive)" strokeWidth="2.5" strokeLinecap="round" />
        </svg>
        <div aria-hidden="true" className="absolute inset-0 grid place-items-center text-center">
          <div>
            <p className="font-mono text-xl leading-none">{c.value_gb !== null ? c.value_gb.toFixed(2) : "n/a"}</p>
            {c.value_gb !== null && <p className="mt-1 text-xs text-muted-foreground">GB</p>}
          </div>
        </div>
      </div>
      <p className="mt-1 text-center text-xs text-muted-foreground">{below ? `Stop below ${c.stop_gb} GB` : `Warn ${c.warn_gb} GB, stop ${c.stop_gb} GB`}</p>
      <div className="mt-2 flex justify-center"><Status tone={st.tone}>{st.label}</Status></div>
      {c.note && <p className="mt-2 text-xs text-muted-foreground">{c.note}</p>}
    </li>
  );
}
