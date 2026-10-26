// Thin client for the XODUS backend. The UI is served by the backend, so all calls are same-origin.

export type Health = {
  backend: { status: string; version: string };
  ollama: { status: string; model: string; message?: string; digest?: string; ollama_version?: string };
};

export type DoneEvent = {
  request_id: string;
  timing: { ttft_ms: number | null; total_ms: number; ollama_load_ms: number };
  usage: { output_tokens: number | null; tokens_per_s: number | null };
  resources: {
    uss_total_mib: number | null;
    uss_ollama_mib: number | null;
    device_vram_peak_mib: number | null;
    device_vram_total_mib: number | null;
  };
};

export type ErrorEvent = { code: string; message: string; request_id?: string; partial?: boolean };

export type StreamHandlers = {
  onMeta?: (data: { request_id: string; model: string; num_ctx: number }) => void;
  onToken: (text: string) => void;
  onDone: (data: DoneEvent) => void;
  onError: (data: ErrorEvent) => void;
};

export async function getHealth(): Promise<Health> {
  const res = await fetch("/api/health", { cache: "no-store" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

/** POST a message and dispatch the server-sent events as they arrive. */
export async function streamChat(message: string, handlers: StreamHandlers, signal: AbortSignal): Promise<void> {
  let res: Response;
  try {
    res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
      signal,
    });
  } catch (err) {
    if (signal.aborted) throw err;
    handlers.onError({ code: "backend_unreachable", message: "Cannot reach the XODUS backend. Is it running?" });
    return;
  }
  if (!res.ok || !res.body) {
    const detail = await res.text().catch(() => "");
    handlers.onError({ code: `http_${res.status}`, message: `The backend rejected the request (HTTP ${res.status}). ${detail}` });
    return;
  }

  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  let finished = false;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    let boundary: number;
    while ((boundary = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      let event = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      const payload = data ? JSON.parse(data) : {};
      if (event === "meta") handlers.onMeta?.(payload);
      else if (event === "token") handlers.onToken(payload.t);
      else if (event === "done") { finished = true; handlers.onDone(payload); }
      else if (event === "error") { finished = true; handlers.onError(payload); }
    }
  }
  if (!finished) {
    handlers.onError({ code: "stream_incomplete", message: "The response stream ended before the backend confirmed completion." });
  }
}

export type ListFilesResult = {
  root: string;
  path: string;
  entries: { name: string; type: "file" | "dir" | "link"; size: number | null }[];
  truncated: boolean;
};

export async function listFiles(path: string): Promise<{ ok: true; result: ListFilesResult } | { ok: false; message: string }> {
  try {
    const res = await fetch("/api/capabilities/fs.list_files/invoke", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ args: { path } }),
    });
    const body = await res.json();
    return body.ok ? { ok: true, result: body.result } : { ok: false, message: body.error?.message ?? `HTTP ${res.status}` };
  } catch {
    return { ok: false, message: "Cannot reach the XODUS backend. Is it running?" };
  }
}
