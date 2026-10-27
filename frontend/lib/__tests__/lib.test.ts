import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, errorFrom } from "../api";
import { splitCitations } from "../citations";
import { ago, bytes, countdown, firstSentence, mib, tplus } from "../format";
import { coerce, fieldsFor } from "../schema-form";
import { parseSseBuffer, streamEvents } from "../sse";
import { MESSAGE_STATUS, TASK_STATUS, TERMINAL_TASK, explainPermission, taskStatus } from "../status";

describe("format", () => {
  it("writes T+ offsets", () => {
    expect(tplus(0)).toBe("T+0.0s");
    expect(tplus(412)).toBe("T+0.4s");
    expect(tplus(59_949)).toBe("T+59.9s");
    expect(tplus(125_300)).toBe("T+2:05.3");
    expect(tplus(-5)).toBe("T+0.0s");
  });
  it("formats sizes and times", () => {
    expect(mib(1536.4)).toBe("1,536 MiB");
    expect(mib(null)).toBe("n/a");
    expect(bytes(512)).toBe("512 B");
    expect(bytes(5 * 1024 ** 2)).toBe("5.0 MiB");
    expect(ago(1000, 1010)).toBe("just now");
    expect(ago(1000, 1000 + 600)).toBe("10 min ago");
    expect(countdown(100, 40)).toBe("1:00");
    expect(countdown(100, 101)).toBe("expired");
  });
  it("finds the first sentence without stopping at abbreviations", () => {
    expect(firstSentence("Evaluate an expression exactly, e.g. (17.5 * 4) / 3. Supports pi.")).toBe("Evaluate an expression exactly, e.g. (17.5 * 4) / 3.");
    expect(firstSentence("One line only")).toBe("One line only");
    expect(firstSentence("Ends here. Then more.")).toBe("Ends here.");
  });
});

describe("citations (same forms as the backend validator)", () => {
  it("splits text and citation groups", () => {
    const parts = splitCitations("Stars form [chunk:3]. Both [chunk:4, chunk:5] and [chunk:6, 7].");
    expect(parts.filter((p) => p.kind === "cite").map((p) => (p as { ids: number[] }).ids)).toEqual([[3], [4, 5], [6, 7]]);
    expect(parts.map((p) => (p.kind === "text" ? p.text : "|")).join("")).toBe("Stars form |. Both | and |.");
  });
  it("accepts spacing and case like the backend, and nothing else", () => {
    expect(splitCitations("[CHUNK : 12 ]")[0]).toMatchObject({ kind: "cite", ids: [12] });
    for (const notACitation of ["[12]", "[chunk:abc]", "(chunk:12)", "[chunk:١٢]", "[chunk:12 chunk:13]", "[citation removed]"]) {
      expect(splitCitations(notACitation)).toEqual([{ kind: "text", text: notACitation }]);
    }
  });
});

describe("api errors", () => {
  it("reads the three error shapes the backend uses", () => {
    expect(errorFrom(409, { ok: false, error: { code: "busy", message: "A task is running." } })).toMatchObject({ code: "busy", message: "A task is running." });
    expect(errorFrom(404, { detail: "No task with that id." })).toMatchObject({ code: "http_404", message: "No task with that id." });
    const v = errorFrom(422, { detail: [{ loc: ["body", "title"], msg: "String should have at least 1 character" }] });
    expect(v.code).toBe("invalid_request");
    expect(v.message).toBe("title: String should have at least 1 character");
    expect(errorFrom(500, "boom").message).toContain("HTTP 500");
  });

  afterEach(() => vi.unstubAllGlobals());

  it("treats a network failure as 'backend unreachable' and ok:false bodies as errors", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    await expect(api("/api/health")).rejects.toMatchObject({ code: "backend_unreachable" });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: false, error: { code: "x", message: "no" } }), { status: 200 })));
    await expect(api("/api/x")).rejects.toBeInstanceOf(ApiError);
  });
});

describe("server-sent events", () => {
  it("parses complete blocks and keeps the rest", () => {
    const { events, rest } = parseSseBuffer('event: token\ndata: {"t":"Hel"}\n\nevent: token\r\ndata: {"t":"lo"}\r\n\r\nevent: done\ndata: {"a"');
    expect(events).toEqual([{ event: "token", data: { t: "Hel" } }, { event: "token", data: { t: "lo" } }]);
    expect(rest).toBe('event: done\ndata: {"a"');
  });
  it("turns a broken event into an error event instead of throwing", () => {
    expect(parseSseBuffer("event: meta\ndata: {not json\n\n").events[0]).toMatchObject({ event: "error", data: { code: "bad_event" } });
  });

  afterEach(() => vi.unstubAllGlobals());

  function streamOf(chunks: string[], status = 200) {
    const body = new ReadableStream<Uint8Array>({
      start(c) { for (const ch of chunks) c.enqueue(new TextEncoder().encode(ch)); c.close(); },
    });
    return new Response(body, { status });
  }

  it("reports a stream that ends without 'done' as unfinished", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(streamOf(['event: meta\ndata: {}\n\nevent: tok', 'en\ndata: {"t":"a"}\n\n'])));
    const seen: string[] = [];
    const r = await streamEvents("/api/chat", { method: "POST", json: {} }, (e) => seen.push(e.event), new AbortController().signal);
    expect(seen).toEqual(["meta", "token"]);
    expect(r.finished).toBe(false);
  });
  it("finishes on 'done' and raises the backend's message on an HTTP error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(streamOf(['event: done\ndata: {"ok":1}\n\n'])));
    expect((await streamEvents("/x", { method: "POST" }, () => {}, new AbortController().signal)).finished).toBe(true);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "No conversation with id 9." }), { status: 404 })));
    await expect(streamEvents("/x", { method: "POST" }, () => {}, new AbortController().signal)).rejects.toMatchObject({ message: "No conversation with id 9." });
  });
});

describe("forms from JSON Schema", () => {
  const textStats = {
    type: "object", additionalProperties: false, required: ["text"],
    properties: { text: { type: "string", minLength: 1, maxLength: 100000 }, top: { type: "integer", minimum: 0, maximum: 20, default: 5 } },
  };
  const listFiles = { type: "object", properties: { path: { type: "string", "x-xodus-path": "fs.read:sandbox", "x-xodus-path-kind": "dir" } } };
  const describeNumbers = { type: "object", required: ["numbers"], properties: { numbers: { type: "array", items: { type: "number" } },
    mode: { type: "string", enum: ["fast", "exact"] }, flag: { type: "boolean" }, extra: { type: "object" } } };

  it("chooses a control per property", () => {
    expect(fieldsFor(textStats).map((f) => [f.name, f.kind, f.required])).toEqual([["text", "text", true], ["top", "integer", false]]);
    const path = fieldsFor(listFiles)[0];
    expect(path.kind).toBe("string");
    expect(path.pathRoot).toBe("sandbox");
    expect(fieldsFor(describeNumbers).map((f) => f.kind)).toEqual(["number-list", "enum", "boolean", "json"]);
  });
  it("coerces values, leaves empty optional fields out and reports problems per field", () => {
    const f = fieldsFor(textStats);
    expect(coerce(f, { text: "hello", top: "3" })).toEqual({ args: { text: "hello", top: 3 }, errors: {} });
    expect(coerce(f, { text: "hello", top: "" }).args).toEqual({ text: "hello" });
    expect(coerce(f, { text: "", top: "2.5" }).errors).toEqual({ text: "Required.", top: "Enter a whole number." });
    expect(coerce(f, { text: "x", top: "21" }).errors.top).toBe("At most 20.");
    const d = fieldsFor(describeNumbers);
    expect(coerce(d, { numbers: "1, 2  3;4", mode: "", flag: true, extra: '{"a":1}' }).args).toEqual({ numbers: [1, 2, 3, 4], flag: true, extra: { a: 1 } });
    expect(coerce(d, { numbers: "1, two", extra: "{" }).errors).toEqual({ numbers: "Enter numbers separated by commas or spaces.", extra: "Not valid JSON." });
  });
});

describe("status words", () => {
  it("covers every status the backend sets and calls only 'completed' a success", () => {
    for (const s of ["queued", "running", "awaiting_approval", "completed", "failed_safe", "blocked", "budget_exceeded", "step_limit", "cancelled", "error"]) {
      expect(TASK_STATUS[s], s).toBeDefined();
    }
    const good = Object.entries(TASK_STATUS).filter(([, v]) => v.tone === "good").map(([k]) => k);
    expect(good).toEqual(["completed"]);
    expect([...TERMINAL_TASK].sort()).toEqual(["blocked", "budget_exceeded", "cancelled", "completed", "error", "failed_safe", "step_limit"]);
    expect(taskStatus("something_new").label).toBe("something_new");
    expect(MESSAGE_STATUS.partial?.label).toMatch(/stopped early/);
  });
  it("explains permissions in plain words", () => {
    expect(explainPermission("fs.write:notes")).toBe('Create or change files inside the "notes" folder (asks for approval).');
    expect(explainPermission("net.none")).toMatch(/not enforced/);
  });
});
