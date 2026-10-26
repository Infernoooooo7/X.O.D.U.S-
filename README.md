# XODUS

Local-first personal AI workspace and research testbed. The governing specification is the XODUS master
prompt v2. Nothing here is deployed or exposed beyond `127.0.0.1`.

**State: Gate D (persistence and knowledge), on top of Gate C (safety and core architecture).**

- **Knowledge:** documents (Markdown, text, CSV, PDF with a text layer, DOCX) are ingested by a supervised worker
  process into one SQLite file (`data/xodus.db`: FTS5 keyword index + `sqlite-vec` vector index), searched by hybrid
  retrieval, and answered from with checked `[chunk:ID]` citations. Deleting a document removes it everywhere, and
  the raw database files are tested to prove it. PostgreSQL was not installed (it needs approval); see
  `docs/STORAGE.md` for the plain-language design and the SQLite-vs-PostgreSQL comparison.
- **Memory:** notes, facts and preferences with provenance; model-derived items start as `proposed` and only a user
  decision makes them `approved`.

- Loopback-only FastAPI backend serving a static UI.
- Streamed chat with a local Ollama model.
- A capability registry with validated manifests, discovered from built-ins, allow-listed entry points
  and out-of-process plugins.
- One enforcement point (`StepGate`) for schemas, permissions, paths and approvals.
- A bounded executor with both H3 arms: ReAct (control, the default) and plan-then-execute (treatment).
- Approvals bound to action hashes, a hash-chained audit log, and a process supervisor using Windows Job
  Objects.

See `docs/THREAT_MODEL.md` for what is enforced, what is tested and what is not.

## Requirements (as tested on this machine)

- Windows 11, NVIDIA GPU (tested: RTX 4050 Laptop, 6 GB, driver 617.14)
- Python 3.14 and [uv](https://docs.astral.sh/uv/) 0.11
- Node.js 22 and npm (only to build the UI; no Node process runs at runtime)
- Ollama 0.40.11 extracted to `%LOCALAPPDATA%\Programs\Ollama` (portable zip, no installer)
- Model `qwen3:4b-instruct-2507-q4_K_M` (digest `0edcdef34593`)

## Setup (once)

```powershell
cd backend;  uv sync;        cd ..
cd frontend; npm ci; npx next build; cd ..   # writes frontend/out/
scripts\start-ollama.ps1
& "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" pull qwen3:4b-instruct-2507-q4_K_M
```

## Run

```powershell
scripts\start-ollama.ps1              # Ollama on 127.0.0.1:11434 with the brief's settings
cd backend; uv run python -m xodus    # backend + UI on http://127.0.0.1:8765
```

Open http://127.0.0.1:8765. Stop with Ctrl+C, then run `scripts\stop-ollama.ps1`, which also stops
Ollama's runner processes.

## Using the knowledge base (Gate D)

Needs the embedding model once: `ollama pull qwen3-embedding:0.6b` (639 MB). Documents come from the folders listed in
`[[permissions.roots]]` (read access); the example config has only `examples/sandbox`.

```powershell
# A small helper (Windows PowerShell mangles inline JSON that contains spaces, so the body goes through a file).
function xodus($path, $body) {
  $f = New-TemporaryFile; ($body | ConvertTo-Json) | Set-Content -Encoding ascii $f
  curl.exe -s -N -X POST "http://127.0.0.1:8765$path" -H "Content-Type: application/json" --data-binary "@$f"; Remove-Item $f
}
# 1. a collection, then queue a file or a whole folder (links are never followed)
xodus /api/knowledge/collections @{ name = "notes" }
xodus /api/knowledge/ingest @{ collection = "notes"; root = "sandbox"; path = "."; recursive = $true }
# 2. watch the jobs (the worker starts by itself, stops after 60 s idle, resumes after a crash)
curl.exe -s http://127.0.0.1:8765/api/knowledge/jobs
# 3. search, or ask with citations (the answer streams)
xodus /api/knowledge/search @{ query = "what is in the sandbox folder" }
xodus /api/knowledge/ask @{ question = "what is in the sandbox folder" }
```

**Speed warning:** with the shipped default `embedding.num_gpu = 0` the embedder runs on the CPU, measured at about
0.5 chunks per second on this machine (a 280-chunk corpus takes roughly nine minutes). On the GPU it ran at about 21
chunks per second but needs about 1 GiB of VRAM next to the generator. Set `embedding.num_gpu = -1` to let Ollama
decide; which setting becomes the default is a Gate G experiment.

To let a *model* search a collection, grant it by name (deny by default):

```toml
[[permissions.collections]]
id = "notes"
grants = ["kb.read"]
```

## API (Gate D additions)

| Endpoint | Purpose |
|---|---|
| `GET/POST /api/knowledge/collections`, `DELETE .../{id}` | Collections (names `[a-z][a-z0-9_-]*`); deleting removes everything in them |
| `POST /api/knowledge/ingest` `{"collection","root","path","recursive"}` | Queue a file or folder: 202 with `jobs` and `rejected` (each with a reason) |
| `GET /api/knowledge/jobs`, `.../{id}`, `POST .../{id}/cancel` | Job status; cancelling stops at the next batch and removes the partial document |
| `GET /api/knowledge/documents`, `.../{id}`, `.../{id}/chunks`, `/api/knowledge/chunks/{id}` | Inspect what was stored (paginated, `limit` at most 200) |
| `DELETE /api/knowledge/documents/{id}` | Delete with a verification of what was removed (refused while its ingestion runs) |
| `POST /api/knowledge/search` `{"query","collections"?,"k"?}` | Hybrid search: ranks from both signals, similarity, `sufficient` |
| `POST /api/knowledge/ask` `{"question","collections"?}` | Server-sent events: `meta` (evidence), `token`, `done` (validated citations, `uncited`, `insufficient`) |
| `GET /api/knowledge/stats` | Counts, file sizes, bytes per chunk, worker state |
| `GET/POST /api/memory/items`, `PATCH`, `POST .../approve`, `.../reject`, `DELETE`, `GET /api/memory/export` | Memory manager |

## API (Gate C additions)

| Endpoint | Purpose |
|---|---|
| `POST /api/tasks` `{"goal", "arm"?}` | Start a task. `arm` is `react` (default, H3 control) or `plan_execute` (H3 treatment). |
| `GET /api/tasks/{id}/events?after=N` | Server-sent events with T+ offsets, until the task ends |
| `POST /api/tasks/{id}/cancel` | Cancel; pending approvals are withdrawn |
| `GET /api/approvals` | Pending approvals: exact action, arguments, targets, undoable, `action_hash` |
| `POST /api/approvals/{id}` `{"decision", "action_hash"}` | Approve or deny; the hash must match what was shown |
| `GET /api/capabilities`, `/api/capabilities/rejected` | Registered capabilities with availability and tier; rejected manifests with reasons |
| `GET /api/processes` | Supervised processes (with job members) and every measured process with its tier |
| `GET /api/audit/verify` | Verify the hash chain of `data/audit.jsonl` |

## Test

```powershell
cd backend
uv run pytest -q                      # everything; live-Ollama, GPU and browser tests skip (with a reason) when unavailable
uv run pytest -q -m "not ollama and not e2e"   # offline subset
```

## Smoke benchmark (master prompt section 6)

Stop the backend and Ollama first; the script takes its own idle baseline and starts both itself.

```powershell
cd backend
uv run python ..\scripts\smoke_benchmark.py
```

Results go to `experiments/results/smoke-<timestamp>/` as `results.json` and `summary.md`.

## Configuration

`config/default.toml` (checked in), optional `config/local.toml` (gitignored), and
`XODUS_<SECTION>__<KEY>` environment variables, for example `XODUS_OLLAMA__NUM_CTX=4096`.
Invalid configuration stops startup with one line per problem.

- Folders that capabilities may touch, and the grants on each, are `[[permissions.roots]]`. By default
  only `examples/sandbox` is configured, and it is read-only.
- Plugins live in `plugins/`; see `plugins/README.md`.

## Logs and data

- `logs/xodus.jsonl`: backend events.
- `logs/tasks.jsonl`: every task event except streamed tokens.
- `logs/ingestion-worker.jsonl`, `logs/ingestion-worker.stderr.log`: the ingestion worker.
- `logs/ollama.*.log`: Ollama's output.
- `data/audit.jsonl`: hash-chained audit log.
- `data/xodus.db` (+ `-wal`, `-shm`): documents, chunks, indexes, jobs, memory. Deleting it resets the knowledge base.
- `data/plugins/<id>/`: plugin work folders.

## Measuring storage at the supported limit

```powershell
cd backend
uv run python ..\scripts\measure_storage.py    # builds 100,000 synthetic chunks and measures size, latency, memory; ~15 min
```
