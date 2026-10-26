"use client";

import { useEffect, useRef, useState } from "react";
import { getHealth, listFiles, streamChat, type DoneEvent, type ErrorEvent, type Health, type ListFilesResult } from "./api";

type Phase = "idle" | "streaming" | "done" | "error";

function HealthStrip() {
  const [health, setHealth] = useState<Health | null>(null);
  const [unreachable, setUnreachable] = useState(false);

  useEffect(() => {
    let alive = true;
    const poll = () =>
      getHealth()
        .then((h) => alive && (setHealth(h), setUnreachable(false)))
        .catch(() => alive && setUnreachable(true));
    poll();
    const id = setInterval(poll, 5000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  if (unreachable) return <p className="strip strip-bad" role="status">Backend · unreachable</p>;
  if (!health) return <p className="strip" role="status">Checking backend…</p>;
  const ok = health.ollama.status === "ok";
  return (
    <p className={`strip ${ok ? "" : "strip-bad"}`} role="status">
      Backend · {health.backend.status} &nbsp;|&nbsp; Ollama · {health.ollama.status}
      {health.ollama.ollama_version ? ` ${health.ollama.ollama_version}` : ""} &nbsp;|&nbsp; {health.ollama.model}
      {health.ollama.digest ? ` · ${health.ollama.digest.slice(0, 12)}` : ""}
    </p>
  );
}

function Chat() {
  const [message, setMessage] = useState("");
  const [reply, setReply] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [done, setDone] = useState<DoneEvent | null>(null);
  const [error, setError] = useState<ErrorEvent | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  async function send(e: React.FormEvent) {
    e.preventDefault();
    if (!message.trim() || phase === "streaming") return;
    const controller = new AbortController();
    abortRef.current = controller;
    setReply(""); setDone(null); setError(null); setPhase("streaming");
    try {
      await streamChat(message, {
        onToken: (t) => setReply((r) => r + t),
        onDone: (d) => { setDone(d); setPhase("done"); },
        onError: (err) => { setError(err); setPhase("error"); },
      }, controller.signal);
    } catch {
      if (controller.signal.aborted) {
        setError({ code: "cancelled", message: "Stopped. The partial reply above is incomplete." });
        setPhase("error");
      }
    }
  }

  return (
    <section className="card" aria-labelledby="chat-title">
      <p className="eyebrow">01 · Assistant</p>
      <h2 id="chat-title">Ask XODUS</h2>
      <form onSubmit={send}>
        <label htmlFor="msg" className="sr-only">Message</label>
        <textarea id="msg" rows={3} value={message} onChange={(e) => setMessage(e.target.value)}
          placeholder="Ask something…" disabled={phase === "streaming"} />
        <div className="row">
          <button type="submit" disabled={phase === "streaming" || !message.trim()}>Send</button>
          {phase === "streaming" && (
            <button type="button" className="secondary" onClick={() => abortRef.current?.abort()}>Stop</button>
          )}
          <span className="meta" aria-live="polite">
            {phase === "streaming" && "Streaming…"}
            {phase === "done" && "Completed"}
          </span>
        </div>
      </form>

      {error && (
        <div className="error" role="alert">
          <strong>{error.code === "provider_unavailable" ? "Ollama is not available" : "Request failed"}</strong>
          <p>{error.message}</p>
          <p className="meta">code: {error.code}{error.request_id ? ` · request ${error.request_id}` : ""}</p>
        </div>
      )}

      {(reply || phase === "streaming") && (
        <div className="reply" aria-live="polite">{reply || "…"}</div>
      )}

      {done && (
        <p className="meta ticket">
          request {done.request_id} · TTFT {done.timing.ttft_ms ?? "–"} ms · total {done.timing.total_ms} ms
          {done.timing.ollama_load_ms > 1000 ? ` (incl. model load ${Math.round(done.timing.ollama_load_ms)} ms)` : ""}
          {" · "}{done.usage.output_tokens ?? "–"} tokens @ {done.usage.tokens_per_s ?? "–"} tok/s
          {" · "}XODUS USS {done.resources.uss_total_mib ?? "–"} MiB (Ollama {done.resources.uss_ollama_mib ?? "–"})
          {" · "}GPU peak {done.resources.device_vram_peak_mib ?? "–"} / {done.resources.device_vram_total_mib ?? "–"} MiB (device-wide)
        </p>
      )}
    </section>
  );
}

function ListFilesCard() {
  const [path, setPath] = useState(".");
  const [result, setResult] = useState<ListFilesResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function run(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true); setError(null); setResult(null);
    const out = await listFiles(path);
    if (out.ok) setResult(out.result); else setError(out.message);
    setBusy(false);
  }

  return (
    <section className="card" aria-labelledby="files-title">
      <p className="eyebrow">02 · Capability · fs.list_files · read-only</p>
      <h2 id="files-title">List files in the sandbox</h2>
      <form onSubmit={run} className="row">
        <label htmlFor="path">Folder (relative to root)</label>
        <input id="path" value={path} onChange={(e) => setPath(e.target.value)} />
        <button type="submit" disabled={busy}>List</button>
      </form>
      {error && <div className="error" role="alert"><p>{error}</p></div>}
      {result && (
        <>
          <p className="meta">{result.root}/{result.path === "." ? "" : result.path} · {result.entries.length} entries{result.truncated ? " (truncated)" : ""}</p>
          <ul className="files">
            {result.entries.map((e) => (
              <li key={e.name}><span className="tag">{e.type}</span> {e.name}{e.size !== null ? ` · ${e.size} B` : ""}</li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

export default function Home() {
  return (
    <main>
      <header>
        <p className="eyebrow">Local · loopback only · Gate B slice</p>
        <h1>XODUS</h1>
        <HealthStrip />
      </header>
      <Chat />
      <ListFilesCard />
    </main>
  );
}
