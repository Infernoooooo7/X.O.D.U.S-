// How backend states are shown. A state is only ever shown as finished or successful when the backend said so.

export type Tone = "neutral" | "active" | "wait" | "good" | "bad";

export const TASK_STATUS: Record<string, { label: string; tone: Tone; explain: string }> = {
  queued: { label: "Queued", tone: "neutral", explain: "Waiting for the model slot." },
  planning: { label: "Planning", tone: "active", explain: "The model is choosing steps." },
  running: { label: "Running", tone: "active", explain: "Steps are being executed." },
  awaiting_approval: { label: "Needs your approval", tone: "wait", explain: "Paused until you approve or deny a step." },
  completed: { label: "Completed", tone: "good", explain: "The backend finished the task." },
  cancelled: { label: "Cancelled", tone: "neutral", explain: "Stopped on request; nothing further runs." },
  blocked: { label: "Blocked", tone: "bad", explain: "A step was refused by the permission checks." },
  failed_safe: { label: "Stopped safely", tone: "bad", explain: "The task could not continue and stopped without side effects." },
  budget_exceeded: { label: "Budget used up", tone: "bad", explain: "A token, model-call or time limit was reached." },
  step_limit: { label: "Step limit reached", tone: "bad", explain: "The task used all the steps it is allowed." },
  error: { label: "Error", tone: "bad", explain: "Something failed; see the message." },
};

export function taskStatus(status: string) {
  return TASK_STATUS[status] ?? { label: status, tone: "neutral" as Tone, explain: "" };
}

export const TERMINAL_TASK = new Set(["completed", "failed_safe", "blocked", "budget_exceeded", "step_limit", "cancelled", "error"]);

export const JOB_STATUS: Record<string, { label: string; tone: Tone }> = {
  queued: { label: "Queued", tone: "neutral" },
  running: { label: "Ingesting", tone: "active" },
  completed: { label: "Done", tone: "good" },
  failed: { label: "Failed", tone: "bad" },
  cancelled: { label: "Cancelled", tone: "neutral" },
};

export const MESSAGE_STATUS: Record<string, { label: string; tone: Tone } | undefined> = {
  complete: undefined, // the normal case needs no badge
  partial: { label: "Incomplete: the reply stopped early", tone: "wait" },
  error: { label: "Failed", tone: "bad" },
  cancelled: { label: "Stopped", tone: "neutral" },
};

export const APPROVAL_STATUS: Record<string, { label: string; tone: Tone }> = {
  pending: { label: "Waiting for you", tone: "wait" },
  approved: { label: "Approved", tone: "good" },
  consumed: { label: "Approved and used", tone: "good" },
  denied: { label: "Denied", tone: "bad" },
  expired: { label: "Expired", tone: "neutral" },
  withdrawn: { label: "Withdrawn (task ended)", tone: "neutral" },
};

/** Plain-language meaning of a permission string from a capability manifest. */
export function explainPermission(p: string): string {
  const [kind, target] = p.split(":", 2);
  switch (kind) {
    case "fs.read": return `Read files inside the "${target}" folder.`;
    case "fs.write": return `Create or change files inside the "${target}" folder (asks for approval).`;
    case "kb.read": return target === "*" ? "Search knowledge collections you have granted." : `Search the "${target}" collection.`;
    case "net.none": return "Declares no network use (declared, not enforced).";
    case "net.outbound": return "Connect to the internet (never granted by default).";
    case "exec.none": return "Declares it starts no programs (declared, not enforced).";
    case "exec.spawn": return "Start other programs (never granted by default).";
    default: return p;
  }
}

export function explainSideEffects(s: string, approval: string): string {
  if (s === "none") return "Changes nothing: only reads or computes.";
  const what = s === "local" ? "Changes things on this computer." : "Acts outside this computer.";
  return approval === "never" ? what : `${what} Each use waits for your approval.`;
}

export function explainMode(mode: string, tier: string): string {
  if (mode === "in_process") return "Built in: runs inside the XODUS backend.";
  if (mode === "mcp") return `MCP server: a separate process (${tier} tier), started on first use and stopped when idle.`;
  return `Plugin: a separate process (${tier} tier), started on first use and stopped when idle.`;
}
