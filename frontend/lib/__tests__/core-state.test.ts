import { describe, expect, it } from "vitest";
import { deriveCore, type CoreInputs } from "../core-state";

const READY: CoreInputs = { backend: "up", ollama: "ok", live: null, tasks: [] };

describe("deriveCore: the core shows only what the backend reported", () => {
  it.each<[string, Partial<CoreInputs>, string, string]>([
    ["nothing running", {}, "idle", "Ready"],
    ["no health answer yet", { backend: "unknown", ollama: null }, "idle", "Connecting"],
    ["request sent, no token yet", { live: { phase: "streaming", chunks: 0 } }, "thinking", "Thinking"],
    ["tokens arriving", { live: { phase: "streaming", chunks: 3 } }, "streaming", "Writing"],
    ["reply finished", { live: { phase: "done", chunks: 40 } }, "idle", "Ready"],
    ["reply failed", { live: { phase: "error", chunks: 0, error: "provider_error" } }, "error", "That did not work"],
    ["reply stopped by the user", { live: { phase: "error", chunks: 5, error: "cancelled" } }, "idle", "Ready"],
    ["a task running", { tasks: ["running"] }, "working", "Working on a task"],
    ["a task queued", { tasks: ["queued"] }, "working", "Working on a task"],
    ["only finished tasks", { tasks: ["completed", "failed_safe", "cancelled"] }, "idle", "Ready"],
    ["a task waiting for approval", { tasks: ["running", "awaiting_approval"] }, "waiting", "Waiting for your approval"],
    ["Ollama down", { ollama: "unreachable" }, "offline", "Ollama is not reachable"],
    ["model runs elsewhere", { ollama: "remote_model" }, "offline", "Model refused"],
    ["model not installed", { ollama: "model_missing" }, "offline", "Model not installed"],
    ["backend down", { backend: "down", ollama: null }, "offline", "Backend not answering"],
  ])("%s", (_name, change, state, label) => {
    const core = deriveCore({ ...READY, ...change });
    expect(core.state).toBe(state);
    expect(core.label).toBe(label);
  });

  it("puts what needs the user first: approval beats streaming, streaming beats a running task", () => {
    expect(deriveCore({ ...READY, live: { phase: "streaming", chunks: 2 }, tasks: ["awaiting_approval"] }).state).toBe("waiting");
    expect(deriveCore({ ...READY, live: { phase: "streaming", chunks: 2 }, tasks: ["running"] }).state).toBe("streaming");
  });

  it("explains an unreachable backend before anything else, and Ollama trouble before an old error", () => {
    expect(deriveCore({ backend: "down", ollama: "ok", live: { phase: "streaming", chunks: 1 }, tasks: ["awaiting_approval"] }).state).toBe("offline");
    expect(deriveCore({ ...READY, ollama: "unreachable", live: { phase: "error", chunks: 0, error: "provider_unavailable" } }).label)
      .toBe("Ollama is not reachable");
  });

  it("counts what actually arrived and names the error code", () => {
    expect(deriveCore({ ...READY, live: { phase: "streaming", chunks: 1 } }).detail).toBe("1 chunk received");
    expect(deriveCore({ ...READY, live: { phase: "streaming", chunks: 42 } }).detail).toBe("42 chunks received");
    expect(deriveCore({ ...READY, live: { phase: "error", chunks: 0, error: "input_too_large" } }).detail).toBe("code input_too_large");
    expect(deriveCore({ ...READY, tasks: ["running", "queued"] }).detail).toBe("2 tasks in progress");
  });
});
