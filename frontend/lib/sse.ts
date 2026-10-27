// Server-sent events over fetch (works for POST streams and for GET task events, and reports errors plainly).

import { ApiError, errorFrom } from "./api";

export type SseEvent = { event: string; data: Record<string, unknown> };

/** Split a buffer into complete SSE blocks; returns the parsed events and the unfinished remainder. */
export function parseSseBuffer(buffer: string): { events: SseEvent[]; rest: string } {
  const events: SseEvent[] = [];
  const normalized = buffer.replace(/\r\n/g, "\n");
  let rest = normalized;
  let boundary: number;
  while ((boundary = rest.indexOf("\n\n")) >= 0) {
    const block = rest.slice(0, boundary);
    rest = rest.slice(boundary + 2);
    let event = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
    }
    if (!data.length) continue;
    try {
      events.push({ event, data: JSON.parse(data.join("\n")) });
    } catch {
      events.push({ event: "error", data: { code: "bad_event", message: "The backend sent an event that is not JSON." } });
    }
  }
  return { events, rest };
}

/**
 * Stream events from `url`. Resolves when the stream ends. `finished` tells whether a terminal event ("done",
 * "error" or one the caller names) arrived: a stream that just stops is reported as incomplete, never as success.
 */
export async function streamEvents(
  url: string,
  init: RequestInit & { json?: unknown },
  onEvent: (e: SseEvent) => void,
  signal: AbortSignal,
  terminal: (e: SseEvent) => boolean = (e) => e.event === "done" || e.event === "error",
): Promise<{ finished: boolean }> {
  const { json, ...rest } = init;
  let res: Response;
  try {
    res = await fetch(url, {
      ...rest,
      signal,
      headers: { ...(json !== undefined ? { "Content-Type": "application/json" } : {}), ...(rest.headers ?? {}) },
      body: json !== undefined ? JSON.stringify(json) : rest.body,
    });
  } catch (err) {
    if (signal.aborted) throw err;
    throw new ApiError(0, "backend_unreachable", "Cannot reach the XODUS backend. Is it running?");
  }
  if (!res.ok || !res.body) {
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      /* not JSON */
    }
    throw errorFrom(res.status, body);
  }
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  let finished = false;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    const parsed = parseSseBuffer(buffer + value);
    buffer = parsed.rest;
    for (const e of parsed.events) {
      if (terminal(e)) finished = true;
      onEvent(e);
    }
  }
  return { finished };
}
