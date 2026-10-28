# XODUS data flows

Status: Gate E, 27 Oct 2026. Required by master prompt v2, section 8: every data flow is listed here.
Labels: **[V]** verified by an executed test, a measurement or a reading of the code named next to it; **[U]** not
verified. Nothing in this file is a guarantee beyond what its evidence shows.

**Summary.** At runtime XODUS sends nothing off this computer by default. The backend has exactly one network
client, the Ollama adapter (`backend/xodus/providers/ollama.py`), and its URL must be a loopback address (the
configuration refuses anything else) with proxy settings ignored (`trust_env=False`) [V: code; `test_config.py`]. Models
that Ollama would run on another computer are refused before anything is sent to them [V: `test_provider.py`,
`test_cloud_guard.py`]. This was not proven by watching all network traffic: no packet capture was taken (see "Not
verified" below).

## 1. Flows between processes on this computer

| # | From → to | What travels | Channel and controls | Evidence |
|---|---|---|---|---|
| 1 | Browser tab → backend | Chat messages, questions, task goals, approval decisions, settings reads, deletions | HTTP on `127.0.0.1:8765`; `Host` allow-list; state changes refused from other origins | [V] `test_api.py`, `test_tasks_api.py`; a Gate E runtime snapshot saw 649 UI requests, all to `127.0.0.1` |
| 2 | Backend → browser tab | The static UI (`frontend/out/`), JSON, streamed replies and task events (SSE) | Same connection | [V] browser tests |
| 3 | Backend → Ollama (`127.0.0.1:11434`) | Chat prompts: system prompt, earlier turns within the history budget, the message. Ask prompts: retrieved passages wrapped as untrusted data. Task prompts: capability schemas, tool output (wrapped, capped). Query embeddings. Model management: `/api/version`, `tags`, `show`, `ps`, `generate` (load and unload) | Loopback only; remote and `-cloud` models refused before sending | [V] `ollama.py` is the only client; `test_provider.py`, `test_cloud_guard.py` |
| 4 | Ingestion worker → Ollama | Text of document chunks, for embeddings (`/api/embed`) | Loopback only; a remote embedding model is refused before any text is sent | [V] `test_cloud_guard.py::test_the_ingestion_embedder_never_sends_text_to_a_remote_model` |
| 5 | Backend → ingestion worker | Its settings as JSON in `XODUS_WORKER_SETTINGS`; it inherits the backend's whole environment (first-party code, unlike plugins). Jobs travel through the database | Child process in a Windows Job Object; reads only configured roots, checked again in the worker | [V] `ingestion/service.py`; `test_ingest_worker.py`, `test_ingest_pipeline.py` |
| 6 | Backend → plugins and MCP servers | Validated arguments; replies capped in size | stdio JSON-RPC; scrubbed environment (no `XODUS_*` variables, no profile paths; a secret only if approved in `plugins.approved_secrets`); own work folder; Job Object; at most 2 at once | [V] `test_plugins.py`, `test_mcp.py` (canary secret absent) |
| 7 | Backend → the Ollama process (read only) | Its listening addresses and its environment, read with psutil; only `OLLAMA_NO_CLOUD`, `OLLAMA_REMOTES` and `OLLAMA_ORIGINS` and the addresses are reported in `/api/health` (values are echoed when unsafe) | Nothing is written to Ollama | [V] `providers/ollama_env.py`; `test_cloud_guard.py`; live check on 27 Oct 2026 |
| 8 | Backend → operating system (read only) | Memory of XODUS-related processes (psutil), device GPU memory (NVML), Windows GPU counters | Shown in the UI; summaries go into request logs | [V] `resources/sampler.py`; `test_sampler.py` |
| 9 | Backend ← files | Configured roots (listing, ingestion), `plugins/` manifests, `config/*.toml`, `experiments/results/` (read-only view) | Paths canonicalised and confined to roots; links refused | [V] `test_paths.py`, `test_list_files.py`, `test_workspace_api.py` |

## 2. What is stored on disk

Paths are relative to the repository unless `data_dir` or `log_dir` are configured elsewhere.

| Location | Contents | Plain text | Removed by |
|---|---|---|---|
| `data/xodus.db` (+ `-wal`) | Ingested document text, chunks, vectors, conversations (questions and replies), memory items, ingestion jobs, model switch records, retrieval metadata (queries stored only as a hash and a length) | Yes, apart from vectors | Deleting a document removes text and vectors from the file (secure delete) [V: `test_deletion.py` raw-file scan]; deleting a conversation removes its messages [V: `test_conversations.py`] |
| `data/audit.jsonl` | Hash-chained records: capability requests and their arguments (secret-looking values redacted), approvals, model loads and switches, process starts and stops, deletions | Yes | Never by XODUS (append-only by design) |
| `logs/xodus.jsonl` | Request ids, model, timing, token counts, resource samples, error messages. Chat records hold the message **length**, not its text | Yes | By hand |
| `logs/tasks.jsonl` | Every task event except streamed tokens: goal, plan, step arguments and targets, previews of tool output, the answer | Yes | By hand; **not** covered by the deletion endpoints |
| `logs/ingestion-worker.jsonl` | Worker events: jobs, progress, errors | Yes | By hand |
| `logs/ollama.out.log`, `logs/ollama.err.log` | Ollama's own server output, when started by `scripts\start-ollama.ps1` | Yes | By hand. Contents are Ollama's [U] |
| `data/plugins/<id>/` | A plugin's `stderr.log` and `tmp/` files | Whatever the plugin writes | By hand |
| `experiments/results/` | Measurement outputs; browser-test screenshots, which can show file paths | Yes | By hand (gitignored) |
| Browser `localStorage` | The theme choice (`xodus-theme`) only | Yes | Browser site data |

## 3. Flows that leave this computer

**At runtime: none by default** [V for XODUS's own code, as above]. Outside XODUS's control:

- **Ollama.** Started by `scripts\start-ollama.ps1` with `OLLAMA_NO_CLOUD=1` on loopback; XODUS checks and reports how the
  running server was started [V]. What `ollama serve` contacts on its own (for example update checks) is **[U]**.
- **Model downloads** happen only when you run `ollama pull`; XODUS has no code that downloads models [V].
- **The browser** you open the UI in is outside XODUS's control.

**At build and development time (started by you, not at runtime):**

- `npm ci` / `uv sync` download packages from the npm registry and PyPI.
- Next.js telemetry is switched off by the project's `npm run build` / `npm run dev` scripts
  (`NEXT_TELEMETRY_DISABLED=1`) [V]. Your user-wide Next.js setting is still "enabled" (your choice), so running
  `npx next build` directly would send telemetry. Whether earlier builds sent it is **[U]** (likely; no capture taken).
- The browser tests drive the installed Google Chrome through Playwright. What headless Chrome contacts during a test
  run is **[U]**.

## 4. Not verified

- No packet capture or firewall log was taken, so "nothing leaves" rests on code inspection (one network client, with
  loopback enforced by the configuration), tests, and one runtime snapshot of connections; it was not observed over time.
- Ollama's own outbound behaviour with `OLLAMA_NO_CLOUD=1`.
- Headless Chrome's outbound traffic during browser tests.
