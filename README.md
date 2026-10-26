# XODUS

Local-first personal AI workspace and research testbed. The governing specification is the XODUS master
prompt v2. Nothing here is deployed or exposed beyond `127.0.0.1`.

**State: Gate C (safety and core architecture).**

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
- `logs/ollama.*.log`: Ollama's output.
- `data/audit.jsonl`: hash-chained audit log.
- `data/plugins/<id>/`: plugin work folders.
