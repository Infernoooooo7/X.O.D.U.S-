"use client";

import { useState } from "react";
import { api, post } from "@/lib/api";
import { ago, bytes, mib } from "@/lib/format";
import { useAction, usePoll } from "@/lib/hooks";
import { Button } from "@/components/ui/button";
import { Confirm } from "@/components/xodus/confirm";
import { Empty, ErrorNote, Facts, NumberedCard, Note, PageHeader, Section, Status, Tag, Ticket } from "@/components/xodus/parts";

type Profile = {
  id: string; label: string; model: string; expected_digest_prefix: string | null; capabilities: string[]; num_ctx: number;
  enabled: boolean; note: string; active: boolean; installed: boolean | null; digest: string | null; digest_ok: boolean | null;
  size_bytes: number | null; loaded: boolean; loaded_size_vram: number | null; measured_vram_mib: number | null;
  measured_at: number | null; measured_num_ctx: number | null;
};
type SwitchRow = { id: number; from_profile: string; to_profile: string; outcome: string; code: string | null; message: string | null;
  duration_ms: number; started_at: number; vram_after_unload: number | null; vram_after_load: number | null };
type Models = { active: string; switching: string | null; profiles: Profile[]; ollama_reachable: boolean; embed_model: string;
  running: { name: string; size: number; size_vram: number }[]; device_vram: { total: number; used: number; free: number } | null;
  recent_switches: SwitchRow[] };

export default function ModelsView() {
  const models = usePoll(() => api<Models>("/api/models"), 5000);
  const action = useAction();
  const [result, setResult] = useState<string | null>(null);
  const [switching, setSwitching] = useState<string | null>(null);

  async function switchTo(p: Profile) {
    setResult(null);
    setSwitching(p.id);
    const r = await action.run(() => post<{ switch: { duration_ms: number; footprint_mib: number | null; unconfirmed_capabilities: string[] } }>(
      "/api/models/switch", { profile: p.id }));
    setSwitching(null);
    if (r) {
      const s = r.switch;
      setResult(`Switched to ${p.label} in ${(s.duration_ms / 1000).toFixed(1)} s. Measured device VRAM for it: ${s.footprint_mib !== null ? mib(s.footprint_mib) : "not available (NVML)"}.` +
        (s.unconfirmed_capabilities.length ? ` Not confirmed by Ollama: ${s.unconfirmed_capabilities.join(", ")}; those features stay off.` : ""));
    }
    void models.refresh();
  }

  const m = models.data;
  return (
    <>
      <PageHeader number="08" title="Models" lead="Generation profiles: which local model answers, with how much context. One generation model is loaded at a time." />
      <Note>
        Every profile is provisional until measured on this machine. A switch unloads the current model, checks that enough
        VRAM is free for the target (once it has been measured), loads it and warms it up. XODUS never downloads a model.
        Switching is refused while a reply or a task is running.
      </Note>
      <ErrorNote error={models.error} title="Profiles cannot be loaded" />
      {m && !m.ollama_reachable && <ErrorNote error={{ code: "provider_unavailable", message: "Ollama is not reachable, so installation and load state are unknown." }}
        hint="Start it with scripts\start-ollama.ps1." />}
      {result && <div className="mt-3"><Note role="status">{result}</Note></div>}
      <ErrorNote error={action.error} title="The switch did not happen" hint="The previous profile stays active." />
      {m && (
        <Ticket fields={[["active", m.active], ["embedding model", m.embed_model],
          ["device VRAM", m.device_vram ? `${mib(m.device_vram.used)} used of ${mib(m.device_vram.total)}` : "n/a"],
          ["loaded now", m.running.map((r) => r.name).join(", ") || "nothing"]]} />
      )}

      <div className="mt-6 grid gap-4 md:grid-cols-2">
        {(m?.profiles ?? []).map((p, i) => {
          const blocked = p.active ? "This is the active profile." : !p.enabled ? `Disabled in the configuration. ${p.note}`
            : p.installed === false ? `Not installed. To use it, run: ollama pull ${p.model}` : p.installed === null ? "Ollama is not reachable." : null;
          return (
            <NumberedCard key={p.id} number={String(i + 1).padStart(2, "0")} eyebrow={<>profile {p.id}</>} title={p.label}
              subtitle={<><code>{p.model}</code>, context {p.num_ctx.toLocaleString()} tokens</>}
              className={p.active ? "border-primary" : undefined}
              tags={<>
                {p.active && <Status tone="good">Active</Status>}
                {p.loaded && <Tag>loaded in Ollama</Tag>}
                {p.installed === false && <Tag>not installed</Tag>}
                {!p.enabled && <Tag>disabled</Tag>}
                {p.capabilities.map((c) => <Tag key={c}>{c}</Tag>)}
              </>}
              action={blocked ? undefined : (
                <Confirm destructive={false} title={`Switch to ${p.label}?`} confirmLabel={`Switch to ${p.label}`}
                  description={<ol className="list-decimal space-y-1 pl-5">
                    <li>Unload the current model and wait until Ollama has dropped it.</li>
                    <li>Check that enough VRAM is free{p.measured_vram_mib ? ` (about ${mib(p.measured_vram_mib)} measured last time)` : " (not measured yet for this profile)"}.</li>
                    <li>Load <code>{p.model}</code> with a {p.num_ctx.toLocaleString()}-token context and warm it up.</li>
                    <li>If loading fails, the current profile stays active.</li>
                  </ol>}
                  onConfirm={() => void switchTo(p)}
                  trigger={<Button size="sm" disabled={action.busy || Boolean(m?.switching)}>{switching === p.id ? "Switching…" : "Switch to this profile"}</Button>} />
              )}>
              <Facts rows={[
                ["installed", p.installed === null ? "unknown" : p.installed ? `yes${p.size_bytes ? `, ${bytes(p.size_bytes)}` : ""}` : "no"],
                ["digest", p.digest ? <span key="d"><code>{p.digest.slice(0, 12)}</code>{p.digest_ok === false ? <span className="text-destructive"> does not match the pin {p.expected_digest_prefix}</span> : p.digest_ok ? " matches the pin" : ""}</span> : "n/a"],
                ["measured VRAM", p.measured_vram_mib !== null ? `${mib(p.measured_vram_mib)} at ${p.measured_num_ctx} context, ${ago(p.measured_at!)}` : "not measured on this machine yet"],
                ["in Ollama now", p.loaded_size_vram !== null ? `${bytes(p.loaded_size_vram)} in VRAM (Ollama's own figure)` : "not loaded"],
              ]} />
              {p.note && <p className="mt-2 text-muted-foreground">{p.note}</p>}
              {blocked && !p.active && <p className="mt-2 font-medium">{blocked}</p>}
            </NumberedCard>
          );
        })}
      </div>

      <Section title="Switch history" id="switches" description="Every attempt, including refusals. VRAM is device-wide (NVML).">
        {(m?.recent_switches ?? []).length === 0 ? <Empty title="No switch attempted yet" /> : (
          <ol className="space-y-2">
            {m!.recent_switches.map((s) => (
              <li key={s.id} className="rounded-md border border-border bg-card p-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{s.from_profile} to {s.to_profile}</span>
                  <Status tone={s.outcome === "switched" ? "good" : s.outcome === "refused" ? "wait" : "bad"}>{s.outcome}</Status>
                  <span className="text-muted-foreground">{ago(s.started_at)}</span>
                </div>
                {s.message && <p className="mt-1">{s.message}</p>}
                <Ticket fields={[["took", `${(s.duration_ms / 1000).toFixed(1)} s`], ["code", s.code ?? ""],
                  ["VRAM after unload", s.vram_after_unload !== null ? bytes(s.vram_after_unload) : ""],
                  ["after load", s.vram_after_load !== null ? bytes(s.vram_after_load) : ""]]} />
              </li>
            ))}
          </ol>
        )}
      </Section>
    </>
  );
}
