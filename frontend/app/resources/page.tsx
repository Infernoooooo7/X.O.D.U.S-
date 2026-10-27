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
          <section aria-labelledby="levels" className="rounded-md border border-border bg-card p-4">
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
                <li key={l.line} className={`rounded-md border border-border bg-card p-3 text-sm ${l.included ? "" : "text-muted-foreground"}`}>
                  <p className="font-medium">{l.label}{!l.included && <> <Tag>not in the total</Tag></>}</p>
                  {l.note && <p className="text-xs text-muted-foreground">{l.note}</p>}
                  <Ticket fields={[["processes", l.processes ?? ""], ["USS", l.uss_mib !== null ? mib(l.uss_mib) : ""],
                    ["working set", l.working_set_mib !== null ? mib(l.working_set_mib) : ""], ["private commit", l.private_commit_mib !== null ? mib(l.private_commit_mib) : ""]]} />
                </li>
              ))}
              <li className="rounded-md border border-border bg-card p-3 text-sm">
                <p className="font-semibold">XODUS-related total</p>
                <Ticket fields={[["USS", `${mib(v.totals.uss_mib)}${v.totals.uss_complete ? "" : " (incomplete)"}`],
                  ["working set", mib(v.totals.working_set_mib)], ["private commit", mib(v.totals.private_commit_mib)]]} />
              </li>
            </ul>
            <div className="hidden rounded-md border border-border bg-card sm:block">
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
            <div className="flex flex-wrap items-center gap-3 rounded-md border border-border bg-card p-4">
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
                  <li key={p.pid} className="rounded-md border border-border bg-card p-3 text-sm">
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

function Meter({ c }: { c: Check }) {
  const st = LEVEL[c.level] ?? LEVEL.unknown;
  const max = c.stop_gb * 1.15;
  const pct = c.value_gb !== null ? Math.min(100, (c.value_gb / max) * 100) : 0;
  const below = c.metric === "system_available";
  return (
    <li className="rounded-sm border border-border p-3">
      <p className="text-sm font-medium">{c.label}</p>
      <p className="mt-1 font-mono text-lg">{c.value_gb !== null ? `${c.value_gb.toFixed(2)} GB` : "n/a"}</p>
      <div className="relative mt-2 h-2 rounded-full bg-muted" role="img"
        aria-label={`${c.value_gb ?? "unknown"} GB; ${below ? `stop below ${c.stop_gb} GB` : `warn at ${c.warn_gb} GB, stop at ${c.stop_gb} GB`}`}>
        <div className="h-2 rounded-full bg-primary" style={{ width: `${pct}%` }} />
        {c.warn_gb !== null && <span className="absolute top-[-3px] h-3.5 w-0.5 bg-warn" style={{ left: `${(c.warn_gb / max) * 100}%` }} />}
        <span className="absolute top-[-3px] h-3.5 w-0.5 bg-destructive" style={{ left: `${(c.stop_gb / max) * 100}%` }} />
      </div>
      <p className="mt-2 text-xs text-muted-foreground">{below ? `Stop below ${c.stop_gb} GB` : `Warn ${c.warn_gb} GB, stop ${c.stop_gb} GB`}</p>
      <div className="mt-1"><Status tone={st.tone}>{st.label}</Status></div>
      {c.note && <p className="mt-1 text-xs text-muted-foreground">{c.note}</p>}
    </li>
  );
}
