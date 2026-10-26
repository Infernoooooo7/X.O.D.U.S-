# Dependencies

Each dependency with one line of justification (master prompt section 10). Versions are pinned in
`backend/uv.lock` and `frontend/package-lock.json`. All are project-local: nothing is installed system-wide.

Per-package memory was **not measured individually**. What was measured is the backend process as a
whole: see "Runtime footprint" at the end.

## Backend runtime (Python 3.14, `backend/pyproject.toml`)

| Package | Version | Why | Runtime memory note |
|---|---|---|---|
| fastapi | 0.162.0 | HTTP API, request validation, SSE responses | in the backend process |
| uvicorn | 0.54.0 | ASGI server bound to 127.0.0.1 | in the backend process |
| pydantic / pydantic-settings | 2.14.1 / 2.16.1 | Typed config with validation; manifest validation | in the backend process |
| httpx | 0.28.1 | Async HTTP client for Ollama (`trust_env=False`: never via a proxy) | in the backend process |
| psutil | 7.2.2 | Per-process USS / working set / private commit; process trees; listener PIDs | in the backend process |
| nvidia-ml-py | 13.615.71 | Device-wide VRAM via NVML (per-process VRAM is unavailable under WDDM) | loads `nvml.dll` |
| pywin32 | 312 | Windows Job Objects (`win32job`) and GPU performance counters (`win32pdh`) | in the backend process |
| jsonschema | 4.26.0 | JSON Schema validation of capability arguments, outputs and plans (Draft 2020-12) | pulls in attrs, referencing, rpds-py, jsonschema-specifications |

## Backend development and tests

| Package | Version | Why |
|---|---|---|
| pytest / pytest-asyncio | 9.1.1 / 1.4.1 | Test runner; async tests |
| playwright (Python) | 1.64.1 | Browser tests driving the **installed** Google Chrome (`channel="chrome"`); no browser download |

## Frontend build (Node 22; never runs at runtime: the UI is a static export served by FastAPI)

| Package | Version | Why |
|---|---|---|
| next | 16.4.3 | Static export of the UI. 16.3.3 was replaced after `npm audit` reported critical advisories fixed in 16.4.3 |
| react / react-dom | 19.3.1 | UI library |
| typescript, @types/* | 7.0.2 | Strict type checking at build time |

Not added yet (planned for later gates, per the stack in section 10): Tailwind CSS, shadcn/ui and Vitest
(Gate E), SQLAlchemy, Alembic and psycopg (Gate D), pypdf and python-docx (Gate D).

## External runtime (not Python or Node packages)

| Component | Version | Source | Size |
|---|---|---|---|
| Ollama (portable zip) | 0.40.11 | github.com/ollama/ollama releases, SHA-256 verified | 1.48 GB download, 2.08 GB extracted |
| qwen3:4b-instruct-2507-q4_K_M | digest 0edcdef34593 | ollama.com library | 2.5 GB |

## Runtime footprint (measured, smoke benchmark of 27 Oct 2026, Gate C code)

The default profile, 20 requests per run, num_ctx 4096 and 8192, two cold-start runs.
Results: `experiments/results/smoke-20261027-010537/`.

| Measure | Run 1 | Run 2 |
|---|---|---|
| Backend processes, peak USS (venv launcher + interpreter) | 81.4 MiB | 80.1 MiB |
| Ollama server + runner, peak USS | 804.5 MiB | 803.8 MiB |
| All XODUS-related processes, peak USS | 884.2 MiB | 883.9 MiB |
| Summed private commit (not physical RAM) | 4,457 MiB | 4,456 MiB |
| Device VRAM increase over idle baseline (NVML) | 3,333 MiB | 3,248 MiB |
| Ollama "shared GPU memory" (Windows counters, system RAM used by the GPU driver) | 442 MiB | 442 MiB |

The benchmark as a whole **failed**: run 1's RAM corroboration and USS completeness checks failed (see the
Gate C report). The per-process USS figures above repeat within 0.1% across runs. Per-package memory is
not measured.
