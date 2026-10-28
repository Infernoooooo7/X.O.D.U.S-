// Requests from outside the workspace (the Ctrl+K palette, quick actions) to start composing: a mode, or a new
// conversation. The workspace listens while it is open; from another view the palette navigates to "/?mode=...".

export type ComposeMode = "chat" | "ask" | "task";
export type ComposeRequest = { mode?: ComposeMode; fresh?: boolean };

export const COMPOSE_EVENT = "xodus:compose";

export function requestCompose(detail: ComposeRequest): void {
  window.dispatchEvent(new CustomEvent<ComposeRequest>(COMPOSE_EVENT, { detail }));
}

export function isComposeMode(value: string | null): value is ComposeMode {
  return value === "chat" || value === "ask" || value === "task";
}
