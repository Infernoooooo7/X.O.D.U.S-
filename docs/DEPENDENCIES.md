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
| sqlite-vec | 0.1.9 | Vector index (`vec0`, exact cosine search, collection filter) loaded as a SQLite extension; the PostgreSQL + pgvector alternative was not approved | a ~0.3 MB DLL loaded into each database connection |
| pypdf | 6.21.2 | PDF text layer, read page by page (pure Python, no native code) | in the ingestion worker only (about 55 MiB peak USS for the worker with all parsers loaded) |

## Backend development and tests

| Package | Version | Why |
|---|---|---|
| pytest / pytest-asyncio | 9.1.1 / 1.4.1 | Test runner; async tests |
| playwright (Python) | 1.64.1 | Browser tests driving the **installed** Google Chrome (`channel="chrome"`); no browser download |
| python-docx (+ lxml) | 1.2.0 (6.1.3) | Creates real DOCX fixtures for the parser tests. **Not used at runtime**: the DOCX parser is XODUS's own streaming reader (zipfile + ElementTree), which keeps memory bounded and lets archive and XML limits be checked first |

Considered and not used: SQLAlchemy and Alembic (the SQLite adapter uses plain parameterised SQL and a small
versioned migration runner; they arrive with the PostgreSQL adapter if that is approved), `python-docx` at runtime
(loads the whole document), `defusedxml` (entity declarations are refused before parsing instead).

## Frontend build (Node 22; never runs at runtime: the UI is a static export served by FastAPI)

| Package | Version | Why |
|---|---|---|
| next | 16.4.3 | Static export of the UI. 16.3.3 was replaced after `npm audit` reported critical advisories fixed in 16.4.3 |
| react / react-dom | 19.3.1 | UI library |
| typescript, @types/* | 7.0.2 | Strict type checking at build time |
| tailwindcss, @tailwindcss/postcss, postcss | 4.3.3, 4.3.3, 8.5.32 | Styling, compiled to plain CSS at build time (Gate E) |
| shadcn | 4.22.2 | shadcn/ui: its CLI wrote the component sources in `components/ui/`; its `tailwind.css` is imported at build time |
| radix-ui | 1.8.1 | Accessible primitives behind those components (dialog, alert dialog, sheet, checkbox, label): focus trapping, Escape, ARIA |
| class-variance-authority, cn | 0.7.1, 0.5.0 | Component variants and class merging used by the shadcn components (`cn` is shadcn's own package, MIT) |
| lucide-react | 1.62.0 | One icon (the mobile menu button); tree-shaken, only used icons are bundled |
| tw-animate-css | 1.4.0 | Open and close transitions of dialogs; disabled under `prefers-reduced-motion` |

All of the above end up as static files in `frontend/out/` (1.36 MB in total, measured after `npm run build`); no Node
process runs at runtime. Fonts are local Windows fonts (Segoe UI Variable, Cascadia Mono): nothing is downloaded. The
shadcn CLI's default preset added the Geist font through `next/font/google`, which downloads it at build time; that
was removed.

## Frontend tests (development only)

| Package | Version | Why |
|---|---|---|
| vitest | 5.0.5 | Unit and component tests (`npm test`) |
| jsdom | 30.1.3 | Browser-like DOM for component tests |
| @testing-library/react, @testing-library/dom | 16.3.3, 10.4.2 | Render components and query them by role and label, as a user or screen reader would |
| axe-core | 4.14.0 | Accessibility rules (WCAG 2.0/2.1/2.2 A and AA) injected into the pages by the Playwright browser tests |

Not used: `@vitejs/plugin-react` (its current version requires Babel 8 while the shadcn CLI needs Babel 7; Vite's
built-in JSX transform is enough), any Playwright for Node (the browser tests use the Python package already
installed in Gate B). Not added yet (planned for later gates, per the stack in section 10): SQLAlchemy, Alembic and
psycopg (only with the PostgreSQL adapter, which was not approved).

## External runtime (not Python or Node packages)

| Component | Version | Source | Size |
|---|---|---|---|
| Ollama (portable zip) | 0.40.11 | github.com/ollama/ollama releases, SHA-256 verified | 1.48 GB download, 2.08 GB extracted |
| qwen3:4b-instruct-2507-q4_K_M | digest 0edcdef34593 | ollama.com library | 2.5 GB |
| qwen3-embedding:0.6b | digest ac6da0dfba84 (checked after the pull) | ollama.com library (approved 27 Oct 2026) | 639 MB download; 1,024-dimension vectors; native context 32,768 |

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
