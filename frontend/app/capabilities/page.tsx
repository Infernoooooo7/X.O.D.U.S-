"use client";

import { useState } from "react";
import { api, post, type Capability } from "@/lib/api";
import { firstSentence } from "@/lib/format";
import { useAction, usePoll } from "@/lib/hooks";
import { explainMode, explainPermission, explainSideEffects } from "@/lib/status";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Empty, ErrorNote, Facts, JsonView, NumberedCard, Note, PageHeader, Section, Status, Tag, Ticket } from "@/components/xodus/parts";
import { SchemaForm } from "@/components/xodus/schema-form";

type Extension = {
  kind: "plugin" | "mcp_server"; id: string; name: string; version: string; description: string; folder: string; command: string[];
  tools: { capability: string; mcp_tool: string | null; disabled_reason: string | null }[];
  process: { pid: number; alive: boolean; tier: string } | null; starts?: number; protocol_version?: string | null;
  server_info?: { name?: string; version?: string } | null; paused: string | null; refused_server_requests?: string[]; secrets: string[];
};
type Extensions = { extensions: Extension[]; rejected: Rejected[]; optional_running: number; max_optional_processes: number;
  idle_timeout_s: number; memory_limit_mib: number | null; plugin_dir: string; note: string };
type Rejected = { source: string; ref: string; reasons: string[] };

export default function Capabilities() {
  const caps = usePoll(() => api<Capability[]>("/api/capabilities"), 10000);
  const ext = usePoll(() => api<Extensions>("/api/extensions"), 5000);
  const rejected = usePoll(() => api<Rejected[]>("/api/capabilities/rejected"), 0);
  const [trying, setTrying] = useState<Capability | null>(null);

  const list = caps.data ?? [];
  const available = list.filter((c) => c.available).length;
  return (
    <>
      <PageHeader number="05" title="Capabilities" lead="Everything XODUS can do for you or for the model, what each one may touch, and whether it needs your approval.">
        <Ticket className="mt-0 border-0 pt-0" fields={[["available", `${available} of ${list.length}`], ["rejected", rejected.data?.length ?? 0]]} />
      </PageHeader>
      <Note>
        A model can only use the capabilities offered to a task, only with arguments that match the schema, and only on the
        folders and collections your configuration grants. Anything that changes files waits for your approval. Nothing a
        model says can add a capability or a permission.
      </Note>
      <ErrorNote error={caps.error} title="The catalogue cannot be loaded" />

      <div className="mt-6 grid gap-4 md:grid-cols-2">
        {list.map((c, i) => {
          const readOnly = c.side_effects === "none" && c.requires_approval === "never";
          return (
            <NumberedCard key={c.id} number={String(i + 1).padStart(2, "0")} eyebrow={<>{c.id} v{c.version}</>} title={c.name}
              subtitle={firstSentence(c.description)}
              tags={<>
                <Tag>{c.execution_mode === "in_process" ? "built in" : c.execution_mode === "mcp" ? "MCP" : "plugin"}</Tag>
                <Tag>tier {c.process_tier}</Tag>
                <Tag>effect {c.side_effects}</Tag>
                <Tag>approval {c.requires_approval}</Tag>
                <Tag>timeout {c.timeout_s}s</Tag>
              </>}
              action={c.available && readOnly
                ? <Button size="sm" variant="outline" onClick={() => setTrying(c)}>Try it</Button>
                : <span className="text-xs text-muted-foreground">{c.available ? "Runs inside tasks" : "Unavailable"}</span>}>
              <div className="space-y-3">
                <Status tone={c.available ? "good" : "bad"}>{c.available ? "Available" : "Unavailable"}</Status>
                {!c.available && (
                  <ul className="list-disc pl-5 text-destructive">{c.unavailable_reasons.map((r) => <li key={r}>{r}</li>)}</ul>
                )}
                <Facts rows={[
                  ["runs", explainMode(c.execution_mode, c.process_tier)],
                  ["changes", explainSideEffects(c.side_effects, c.requires_approval)],
                  ["may", <ul key="p" className="space-y-0.5">{c.permissions.map((p) => <li key={p}><code>{p}</code>: {explainPermission(p)}</li>)}</ul>],
                  ["source", c.source],
                ]} />
                <details>
                  <summary className="cursor-pointer text-xs text-muted-foreground">Full description and schemas</summary>
                  <p className="mt-2">{c.description}</p>
                  <p className="mt-2 text-xs font-medium">Input</p><JsonView value={c.input_schema} label={`${c.id} input schema`} />
                  <p className="mt-2 text-xs font-medium">Output</p><JsonView value={c.output_schema} label={`${c.id} output schema`} />
                </details>
              </div>
            </NumberedCard>
          );
        })}
      </div>

      <Section title="Extensions" id="extensions" description={ext.data?.note}>
        <ErrorNote error={ext.error} />
        {ext.data && (
          <Ticket className="mb-3 mt-0" fields={[["optional processes running", `${ext.data.optional_running} of ${ext.data.max_optional_processes}`],
            ["idle stop", `${Math.round(ext.data.idle_timeout_s / 60)} min`], ["memory limit each", ext.data.memory_limit_mib ? `${ext.data.memory_limit_mib} MiB` : "none"],
            ["folder", ext.data.plugin_dir]]} />
        )}
        {ext.data && ext.data.extensions.length === 0 && <Empty title="No plugins or MCP servers are installed">Put one in the plugins folder and restart the backend.</Empty>}
        <div className="grid gap-4 md:grid-cols-2">
          {(ext.data?.extensions ?? []).map((e) => (
            <article key={e.id} className="rounded-md border border-border bg-card p-4 text-sm">
              <p className="eyebrow">{e.kind === "mcp_server" ? "MCP server" : "plugin"} {e.id} v{e.version}</p>
              <h3 className="mt-0.5 text-base font-semibold">{e.name}</h3>
              <p className="text-muted-foreground">{e.description}</p>
              <div className="mt-3 flex flex-wrap gap-2">
                {e.process?.alive
                  ? <Status tone="active">Running, pid {e.process.pid}</Status>
                  : <Status tone="neutral">Not running (starts on first use)</Status>}
                {e.paused && <Status tone="bad">{e.paused}</Status>}
              </div>
              <Facts rows={[
                ["command", <code key="c" className="[overflow-wrap:anywhere]">{e.command.join(" ")}</code>],
                ["folder", <code key="f" className="[overflow-wrap:anywhere]">{e.folder}</code>],
                ["provides", <ul key="t">{e.tools.map((t) => (
                  <li key={t.capability}><code>{t.capability}</code>{t.mcp_tool ? <> (tool <code>{t.mcp_tool}</code>)</> : null}
                    {t.disabled_reason && <span className="block text-destructive">{t.disabled_reason}</span>}</li>))}</ul>],
                ...(e.kind === "mcp_server" ? [["protocol", e.protocol_version ? `${e.protocol_version}, server ${e.server_info?.name ?? "?"} ${e.server_info?.version ?? ""}` : "not connected yet"] as [string, string],
                  ["tool pins", e.protocol_version ? (e.tools.some((t) => t.disabled_reason) ? "a definition changed: see above" : "match the reviewed manifest") : "checked on first connection"] as [string, string]] : []),
                ...(e.refused_server_requests?.length ? [["refused requests", e.refused_server_requests.join(", ")] as [string, string]] : []),
                ["secrets", e.secrets.length ? e.secrets.join(", ") : "none"],
                ["starts", String(e.starts ?? (e.process ? 1 : 0))],
              ]} />
            </article>
          ))}
        </div>
      </Section>

      <Section title="Rejected" id="rejected" description="Manifests and extensions that failed validation. Nothing of them is loaded.">
        {(rejected.data ?? []).length === 0 ? <Empty title="Nothing was rejected" /> : (
          <ul className="space-y-2">
            {(rejected.data ?? []).map((r) => (
              <li key={`${r.source}:${r.ref}`} className="rounded-md border border-destructive/40 bg-card p-3 text-sm">
                <p className="eyebrow">{r.source}</p>
                <p className="font-medium">{r.ref}</p>
                <ul className="list-disc pl-5 text-destructive">{r.reasons.map((x) => <li key={x}>{x}</li>)}</ul>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <TryDialog cap={trying} onClose={() => setTrying(null)} />
    </>
  );
}

function TryDialog({ cap, onClose }: { cap: Capability | null; onClose: () => void }) {
  const action = useAction();
  const [result, setResult] = useState<{ result: unknown; duration_ms: number } | null>(null);
  return (
    <Dialog open={cap !== null} onOpenChange={(o) => { if (!o) { onClose(); setResult(null); action.setError(null); } }}>
      <DialogContent className="max-h-[88dvh] overflow-y-auto sm:max-w-xl">
        {cap && (
          <>
            <DialogHeader>
              <DialogTitle>{cap.name}</DialogTitle>
              <DialogDescription>{cap.description}</DialogDescription>
            </DialogHeader>
            <SchemaForm schema={cap.input_schema} idPrefix={`try-${cap.id}`} submitLabel={`Run ${cap.id}`} busy={action.busy}
              onSubmit={async (args) => {
                setResult(null);
                const r = await action.run(() => post<{ result: unknown; duration_ms: number }>(`/api/capabilities/${cap.id}/invoke`, { args }));
                if (r) setResult(r);
              }} />
            <ErrorNote error={action.error} title="The call was refused or failed" />
            {result && (
              <div role="status">
                <p className="mt-2 text-sm font-medium">Result in {Math.round(result.duration_ms)} ms</p>
                <JsonView value={result.result} label="Result" />
              </div>
            )}
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
