"use client";

import { api } from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { useTheme, type ThemeChoice } from "@/lib/theme";
import { ErrorNote, Facts, JsonView, Note, PageHeader, Section, Tag } from "@/components/xodus/parts";

type SettingsView = {
  config_files: string[]; config_hash: string; how_to_change: string; server: { host: string; port: number };
  roots: { id: string; path: string; grants: string[] }[]; collection_grants: { id: string; grants: string[] }[];
  global_grants: string[]; envelope: Record<string, number>; executor: Record<string, unknown>; approvals: { ttl_s: number };
  thresholds: Record<string, number>; embedding: Record<string, unknown>; knowledge_limits: Record<string, number>;
  data_dir: string; log_dir: string; effective: unknown;
};

export default function Settings() {
  const s = usePoll(() => api<SettingsView>("/api/settings"), 0);
  const { choice: theme, set: choose } = useTheme();

  const v = s.data;
  return (
    <>
      <PageHeader number="11" title="Settings" lead="How this XODUS is configured. Configuration is read-only here: permissions are never granted from a web page." />

      <section aria-labelledby="appearance" className="panel p-4 sm:p-5">
        <h2 id="appearance" className="text-lg font-semibold">Appearance</h2>
        <fieldset className="mt-2">
          <legend className="text-sm text-muted-foreground">Theme (saved in this browser only; also on the top bar and in Ctrl+K)</legend>
          <div className="mt-2 flex flex-wrap gap-4">
            {(["dark", "light", "system"] as ThemeChoice[]).map((t) => (
              <label key={t} className="flex items-center gap-2 text-sm">
                <input type="radio" name="theme" value={t} checked={theme === t} onChange={() => choose(t)} className="size-4 accent-[var(--primary)]" />
                {t === "system" ? "Follow Windows" : t === "light" ? "Light" : "Dark (default)"}
              </label>
            ))}
          </div>
        </fieldset>
      </section>

      <ErrorNote error={s.error} title="Settings cannot be loaded" />
      {v && (
        <>
          <div className="mt-6"><Note>{v.how_to_change}</Note></div>
          <Section title="Folders and grants" id="roots" description="The only places capabilities may touch, and what they may do there.">
            <ul className="space-y-2">
              {v.roots.map((r) => (
                <li key={r.id} className="panel p-3 text-sm">
                  <p className="font-medium">{r.id} {r.grants.map((g) => <Tag key={g}>{g === "fs.read" ? "read" : "write, with approval"}</Tag>)}</p>
                  <p className="font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]">{r.path}</p>
                </li>
              ))}
            </ul>
            <div className="mt-3">
              <Facts rows={[
                ["collections the model may search", v.collection_grants.length ? v.collection_grants.map((c) => c.id).join(", ") : "none (deny by default)"],
                ["global grants", v.global_grants.length ? v.global_grants.join(", ") : "none: no network, no starting programs"],
                ["approvals expire after", `${Math.round(v.approvals.ttl_s / 60)} min`],
              ]} />
            </div>
          </Section>
          <Section title="Limits" id="limits">
            <Facts rows={[
              ["server", `${v.server.host}:${v.server.port} (loopback only)`],
              ["generation", `${v.envelope.max_running} running, ${v.envelope.max_queued} queued`],
              ["tasks", `${v.executor.max_active_tasks} at once, default strategy ${v.executor.default_arm}`],
              ["files", `${v.knowledge_limits.max_file_mb} MB each, ${v.knowledge_limits.max_pdf_pages} PDF pages, ${v.knowledge_limits.max_chunks.toLocaleString()} passages in total`],
              ["thresholds", `warn ${v.thresholds.warn_uss_gb} GB RAM or ${v.thresholds.warn_vram_gb} GB VRAM; stop ${v.thresholds.stop_uss_gb} GB or ${v.thresholds.stop_vram_gb} GB`],
              ["embedder", `${String(v.embedding.dimensions)} dimensions, ${v.embedding.num_gpu === 0 ? "on the CPU" : "placement chosen by Ollama"}`],
            ]} />
          </Section>
          <Section title="Files" id="files">
            <Facts rows={[["configuration", v.config_files.join(", ")], ["configuration hash", <code key="h">{v.config_hash}</code>],
              ["data", v.data_dir], ["logs", v.log_dir]]} />
            <details className="mt-3">
              <summary className="cursor-pointer text-sm">Show the full effective configuration</summary>
              <JsonView value={v.effective} label="Effective configuration" />
            </details>
          </Section>
        </>
      )}
    </>
  );
}
