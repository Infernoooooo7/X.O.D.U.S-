// Thin client for the XODUS backend. The UI is served by the backend, so every call is same-origin.

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public body?: unknown) {
    super(message);
  }
}

/** The backend answers errors in three shapes; turn each into one readable message. */
export function errorFrom(status: number, body: unknown): ApiError {
  if (body && typeof body === "object") {
    const b = body as Record<string, unknown>;
    const err = b.error as { code?: string; message?: string } | undefined;
    if (err && typeof err === "object" && err.message) return new ApiError(status, err.code ?? `http_${status}`, err.message, body);
    if (typeof b.detail === "string") return new ApiError(status, `http_${status}`, b.detail, body);
    if (Array.isArray(b.detail)) {
      const parts = (b.detail as { loc?: unknown[]; msg?: string }[]).map((d) => {
        const where = (d.loc ?? []).filter((p) => p !== "body").join(".");
        return where ? `${where}: ${d.msg}` : String(d.msg);
      });
      return new ApiError(status, "invalid_request", parts.join("; "), body);
    }
  }
  return new ApiError(status, `http_${status}`, `The backend answered HTTP ${status}.`, body);
}

export async function api<T>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const { json, ...rest } = init;
  let res: Response;
  try {
    res = await fetch(path, {
      cache: "no-store",
      ...rest,
      headers: { ...(json !== undefined ? { "Content-Type": "application/json" } : {}), ...(rest.headers ?? {}) },
      body: json !== undefined ? JSON.stringify(json) : rest.body,
    });
  } catch {
    throw new ApiError(0, "backend_unreachable", "Cannot reach the XODUS backend. Is it running?");
  }
  const text = await res.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!res.ok) throw errorFrom(res.status, body);
  if (body && typeof body === "object" && (body as { ok?: unknown }).ok === false) throw errorFrom(res.status, body);
  return body as T;
}

export const post = <T>(path: string, json?: unknown) => api<T>(path, { method: "POST", json: json ?? {} });
export const patch = <T>(path: string, json: unknown) => api<T>(path, { method: "PATCH", json });
export const del = <T>(path: string) => api<T>(path, { method: "DELETE" });

// ---------------------------------------------------------------------------------------------- shared types

export type Health = {
  backend: { status: string; version: string; uptime_s: number };
  ollama: { status: string; model: string; message?: string; digest?: string; ollama_version?: string };
  models: { active: string; switching: string | null; num_ctx: number };
  envelope: { max_running: number; max_queued: number; running: number; queued: number };
  measurement: { nvml: string; gpu_counters: string };
  knowledge: { embedding: { status: string; model: string; message?: string }; ingestion: IngestionStatus };
};

export type IngestionStatus = {
  worker: { running: boolean; pid: number | null; starts_total: number; degraded: string | null; idle_timeout_s: number };
  pending_jobs: number;
  paused: boolean;
};

export type Conversation = {
  id: number; title: string; created_at: number; updated_at: number; messages: number; last_message?: string | null;
};

export type Citations = { valid: number[]; invalid: number[] };

export type Message = {
  id: number; conversation_id: number; role: "user" | "assistant"; mode: "chat" | "ask" | "task"; content: string;
  status: "complete" | "partial" | "error" | "cancelled"; model: string | null; request_id: string | null;
  task_id: string | null; created_at: number;
  meta: {
    code?: string; message?: string; citations?: Citations; insufficient?: boolean; uncited?: boolean;
    evidence?: { chunk_id: number; document: string; collection?: string; locator?: string | null; heading?: string | null }[];
    task_status?: string; timing?: { ttft_ms: number | null; total_ms: number }; usage?: { output_tokens: number | null };
    [key: string]: unknown;
  };
};

export type ConversationDetail = Conversation & { items: Message[] };

export type Chunk = {
  id: number; document_id: number; document: string; collection: string; ordinal: number; text: string;
  heading_path: string | null; locator: string | null; page: number | null;
};

export type Capability = {
  id: string; name: string; description: string; version: string; input_schema: JsonSchema; output_schema: JsonSchema;
  permissions: string[]; dependencies: string[]; execution_mode: "in_process" | "subprocess" | "mcp";
  process_tier: "none" | "optional"; resource_class: string; timeout_s: number; max_output_bytes: number;
  requires_approval: "never" | "consequential" | "always"; side_effects: "none" | "local" | "external";
  undoable: boolean | null; source: string; available: boolean; unavailable_reasons: string[]; mcp_tool?: string;
};

export type JsonSchema = {
  type?: string | string[]; properties?: Record<string, JsonSchema>; required?: string[]; enum?: unknown[];
  description?: string; default?: unknown; minimum?: number; maximum?: number; minLength?: number; maxLength?: number;
  items?: JsonSchema; minItems?: number; maxItems?: number; additionalProperties?: boolean;
  [key: string]: unknown;
};

export type Approval = {
  id: string; task_id: string; step_index: number; capability_id: string; capability_name: string;
  capability_version: string; args: unknown; targets: string[]; side_effects: string; undoable: boolean | null;
  reasons: string[]; action_hash: string; created_at: number; expires_at: number; status: string; decided_at: number | null;
};

export type TaskStep = {
  index: number; capability_id: string; args: unknown; origin: string; status: string; code: string | null;
  message: string | null; approval_id: string | null; duration_ms: number | null; output_bytes: number | null;
};

export type Task = {
  id: string; goal: string; arm: string; status: string; message: string | null; answer: string | null;
  offered_capabilities: string[]; tainted: boolean; steps: TaskStep[]; budget: Record<string, unknown> | null;
  created_at: number; events: number;
};

export type TaskEvent = { seq: number; t_ms: number; type: string; [key: string]: unknown };
