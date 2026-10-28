// What the core (the animated orb in the workspace) shows. Every state comes from something the backend reported:
// the health poll, the live reply's own events, or a task's event stream. Nothing here is decorative.

export type CoreState = "idle" | "thinking" | "streaming" | "working" | "waiting" | "error" | "offline";

export type CoreInputs = {
  backend: "up" | "down" | "unknown";
  /** health.ollama.status; null until the first health answer. */
  ollama: string | null;
  /** The reply being streamed in this page: its phase, how many token chunks have arrived, the error code if it failed. */
  live: { phase: "streaming" | "done" | "error"; chunks: number; error?: string } | null;
  /** Statuses of this session's tasks, as their timelines report them (queued, running, awaiting_approval, ...). */
  tasks: string[];
};

export type Core = { state: CoreState; label: string; detail: string };

const OLLAMA_OFFLINE: Record<string, [string, string]> = {
  unreachable: ["Ollama is not reachable", "Start it with scripts\\start-ollama.ps1."],
  remote_model: ["Model refused", "The configured model would run on another computer."],
  model_missing: ["Model not installed", "XODUS never downloads models; see the Models view."],
  digest_mismatch: ["Model digest does not match", "The installed model is not the pinned one."],
};

const IN_PROGRESS = new Set(["queued", "planning", "running"]);

/** Highest priority first: what needs the user, then what is happening now, then what is wrong, then ready. */
export function deriveCore({ backend, ollama, live, tasks }: CoreInputs): Core {
  if (backend === "down") return { state: "offline", label: "Backend not answering", detail: "Start it with uv run python -m xodus." };
  if (tasks.includes("awaiting_approval")) {
    return { state: "waiting", label: "Waiting for your approval", detail: "A task is paused until you decide." };
  }
  if (live?.phase === "streaming") {
    return live.chunks > 0
      ? { state: "streaming", label: "Writing", detail: `${live.chunks} chunk${live.chunks === 1 ? "" : "s"} received` }
      : { state: "thinking", label: "Thinking", detail: "Waiting for the first token" };
  }
  const running = tasks.filter((t) => IN_PROGRESS.has(t)).length;
  if (running) return { state: "working", label: "Working on a task", detail: `${running} task${running === 1 ? "" : "s"} in progress` };
  if (ollama !== null && ollama !== "ok") {
    const [label, detail] = OLLAMA_OFFLINE[ollama] ?? [`Ollama: ${ollama.replaceAll("_", " ")}`, "See the system status."];
    return { state: "offline", label, detail };
  }
  if (live?.phase === "error" && live.error !== "cancelled") {
    return { state: "error", label: "That did not work", detail: `code ${live.error ?? "error"}` };
  }
  if (backend === "unknown" || ollama === null) return { state: "idle", label: "Connecting", detail: "Waiting for the first health check" };
  return { state: "idle", label: "Ready", detail: "Send a message to begin" };
}

/** Pulses from real events (a token arrived, a task step started) that make the core flare for a moment. */
export type CorePulse = "token" | "step";
const channel = typeof window === "undefined" ? null : new EventTarget();

export function pulseCore(kind: CorePulse): void {
  channel?.dispatchEvent(new CustomEvent<CorePulse>("pulse", { detail: kind }));
}

export function onCorePulse(listener: (kind: CorePulse) => void): () => void {
  if (!channel) return () => {};
  const handler = (e: Event) => listener((e as CustomEvent<CorePulse>).detail);
  channel.addEventListener("pulse", handler);
  return () => channel.removeEventListener("pulse", handler);
}
