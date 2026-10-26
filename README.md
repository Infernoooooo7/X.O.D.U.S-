# XODUS

Local-first personal AI workspace and research testbed. This is the **Gate B vertical slice**:
a loopback-only FastAPI backend that streams answers from a local Ollama model over SSE,
runs one read-only capability (`fs.list_files`), and logs request timing plus a resource sample.

The governing specification is the XODUS master prompt v2. Nothing here is deployed or exposed
beyond `127.0.0.1`.

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

Open http://127.0.0.1:8765. Stop with Ctrl+C, then `scripts\stop-ollama.ps1`.

## Test

```powershell
cd backend
uv run pytest -v                      # all tests; live-Ollama tests skip (with reason) if Ollama is down
uv run pytest -v -m "not ollama"      # offline subset
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
`XODUS_<SECTION>__<KEY>` environment variables, e.g. `XODUS_OLLAMA__NUM_CTX=4096`.
Invalid configuration stops startup with one line per problem. Non-loopback hosts are rejected.

## Logs

`logs/xodus.jsonl`: one JSON object per line (`startup`, `chat_request`, `capability_call`, `shutdown`).
Ollama's own output is in `logs/ollama.*.log`.
