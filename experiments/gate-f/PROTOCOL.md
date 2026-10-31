# Gate F protocol: reliability and resource evaluation

Written 29 Oct 2026, **before** any Gate F measurement was taken. Changes after the first measured run go in the
amendments section at the end, dated, with the reason; results are reported against this text as well.

Governing text: master prompt v2, sections 3 (resource budgets), 6 (smoke benchmark), 8 (security table), 13 (Gate F).

## 1. Decisions taken before measuring (user, 29 Oct 2026)

| Question | Decision |
|---|---|
| What the VRAM thresholds (warn 4.2 GB, stop 4.8 GB) compare against | **XODUS's own VRAM**: the sum of dedicated GPU memory of XODUS-related processes from the Windows `GPU Process Memory` counters, corroborated by the device-wide NVML increase over the idle baseline. If the counters cannot be read, the check falls back to device-wide use and says so. |
| How the low-memory path is tested | **Both**: lowered-threshold runs (deterministic), and one bounded live ballast run (available memory never pushed below 1.2 GB, held at most 60 s) after all other measurements. |
| Per-install API token (security table) | Implemented in Gate F and tested. |

## 2. Machine and fixed conditions

Recorded in every result file: Windows build, CPU, RAM total, GPU name, driver and driver model, power plan, AC power,
Python and psutil versions, Ollama version, model tags and digests, configuration hash.

- On AC power, Windows power plan unchanged (Balanced). Laptop lid open.
- Ollama started only by `scripts/start-ollama.ps1` (loopback, `NUM_PARALLEL=1`, `MAX_LOADED_MODELS=2`, `MAX_QUEUE=8`,
  flash attention, `q8_0` KV cache, keep-alive 10 min, `NO_CLOUD=1`).
- Shipped configuration (`config/default.toml`), except, for measurement runs only and set through environment variables
  (no file is changed): an isolated `data_dir` and `log_dir` inside the run's result folder (the user's `data/` is never
  touched); one extra read-only root for the fixed corpus; one extra model profile `measure-switch` using the already
  installed `qwen3:4b-instruct-2507-q4_K_M` (no download), used only by the model-switching scenario.
- Other applications: the user's desktop stays as it is (editor, browser). That is real noise, so the idle baseline is
  checked for quietness (section 4) instead of assumed.

## 3. What is measured (sampling)

One sampler process (the harness), two threads:

- every **200 ms**: device VRAM (NVML `used`, `free`);
- every **500 ms**: per process: working set (psutil `rss`), private working set / USS (`memory_full_info().uss`),
  private commit (`vms`); system physical memory in use (`total - available`); every **1 s** (every second slow tick):
  Windows `GPU Process Memory` dedicated and shared usage per process on the discrete adapter, and Ollama `/api/ps`.

Processes included (XODUS-related): the backend and every process in its tree (ingestion worker, plugin and MCP
processes, which the supervisor puts in Job Objects under the backend), and the Ollama server with every runner it
spawned (found by the process listening on port 11434). Each sample names each process with its group:

| Line | Group | In the XODUS aggregate |
|---|---|---|
| 1 | XODUS application: backend, ingestion worker | yes |
| 2 | Ollama server and runners | yes |
| 3 | Supporting services: none (SQLite runs inside the backend; PostgreSQL not installed) | yes (empty) |
| 4 | Optional helpers and plugins (MCP servers, plugin processes) | yes |
| 5 | Total system physical memory in use (all applications) | reference only |
| 6 | Browser tab | **excluded**; not run by the harness (no browser), reported as "not measured" |

The harness's own memory is reported on its own line and excluded from the aggregate.

**USS read failures.** A USS read that fails (`AccessDenied`) is retried once in the same tick. A sample in which any
process's USS is still unreadable is marked incomplete and excluded from USS statistics. A run passes the completeness
check if at least 98% of samples in every scenario window are complete; the count is reported.

**Shared GPU memory.** Shared GPU memory of XODUS processes is system RAM used on the GPU's behalf (spill or pinned
staging memory). It is reported as its own figure and added to the RAM check of the resource governor
("USS + shared GPU memory"). Whether it is already inside USS is checked, not assumed (section 6).

## 4. Idle baseline

With XODUS fully stopped (no backend, no Ollama, ports 8765 and 11434 free): sample for 120 s. The baseline is
**quiet** if the spread (max - min) of system memory in use is at most 256 MiB and of device VRAM at most 128 MiB.
A noisy baseline is repeated, at most 3 times; if none is quiet, the run continues, the least noisy baseline is used,
and every system-delta comparison in that run is reported as **inconclusive (noisy baseline)**, never as a pass.
A second 60 s baseline is taken after everything is stopped at the end of the run and reported (drift check).

## 5. Scenarios (one backend lifetime per run, in this order)

| # | Scenario | Workload | Cold / warm |
|---|---|---|---|
| S1 | Startup | start Ollama, start the backend, wait for `/api/health` ok | cold |
| S2 | Normal chat | the 10 fixed prompts of `experiments/smoke/prompts.json` at `num_ctx` 8192, then 3 of them at 16384 (envelope maximum, "where measured to fit") | first request cold (model load), rest warm |
| S3 | Idle 5 minutes | nothing for 300 s, model left loaded (keep-alive 10 min) | warm |
| S4 | One plugin execution | `text.stats` and `calc.evaluate`: first call (process start) and one warm call each | cold then warm |
| S5 | Three concurrent requests at the queue limit | 3 chats started together (1 runs, 2 queue), then an overflow burst of 6 (1 running + 4 queued + at least 1 refused with `queue_full`) | warm |
| S6 | Cancellation mid-stream | a long chat, client disconnects after 20 tokens; then one normal chat must succeed | warm |
| S7 | Ingestion of the fixed 50-document corpus | `experiments/corpus/gate-f-50/` (MANIFEST.json pins every file's SHA-256), embedder at the shipped placement (`embedding.num_gpu = 0`, CPU) | cold embedder |
| S8 | Embedding generation | 20 fixed knowledge searches (each embeds its query) against the ingested corpus | warm embedder |
| S9 | Retrieval | 10 fixed knowledge questions answered with citations (`/api/knowledge/ask`) | warm |
| S10 | Model switching | `default` (qwen3.5:4b) to `measure-switch` (qwen3:4b-instruct) and back, each with warm-up; `/api/ps` must never show two generation models | cold loads |

For each scenario window the record holds: start, end, duration, included processes with tiers, steady value (median
over the window), peak, per-line USS / working set / private commit, shared GPU memory, device VRAM peak and increase
over baseline, per-process dedicated GPU memory, `/api/ps`, latency (time to first token, total, tokens per second),
errors, and the sampler's own CPU and memory. Sampling intervals as in section 3.

Each full run is executed **twice** (run A, run B) with the same commands.

## 6. Pass rules for the measurement pipeline (fixed now)

| Check | Rule |
|---|---|
| RAM corroboration | Run peak of summed USS vs run peak of system in-use increase over the idle baseline: differ by at most 20% of the larger. If not, investigate the gap (shared GPU memory, mapped model files, other applications) and report the explanation; the check itself stays failed. |
| VRAM corroboration | Device-wide NVML increase over baseline vs summed per-process dedicated GPU memory of XODUS processes: differ by at most max(15%, 300 MiB). (Tolerance chosen by the harness author in Gate B; the brief gives no number.) |
| Repeatability | Run A vs run B: peak summed USS and peak device VRAM **increase over each run's idle baseline** within 5% (the raw device figure includes the desktop's share, which moved between 1.1 and 1.9 GB on this machine across sessions; the raw peak is reported too). |
| Completeness | Section 3: at least 98% complete samples per scenario. |
| Sampler overhead | Harness sampler threads below 2% of total CPU capacity. |

The smoke benchmark (section 6 of the brief: default profile, 10 prompts, `num_ctx` 4096 and 8192, two cold runs) must
pass these rules **before** any scenario run is used for a budget decision.

## 7. Targets and how each is judged

Every target ends as **met**, **missed** or **not measured**, from measurements only. GB = 10^9 bytes.

| Target (brief) | Measured as | Met if |
|---|---|---|
| XODUS RAM goal about 4 GB | peak summed USS over all scenarios | <= 4.0 GB |
| XODUS RAM ceiling 6 GB (supported envelope) | same, plus shared GPU memory | <= 6.0 GB |
| XODUS VRAM about 4.0-4.5 GB | peak device VRAM increase over baseline | <= 4.5 GB |
| Ollama server + generation runner RAM <= 1.0 GB, VRAM <= 4.0 GB | Ollama line USS; per-process dedicated GPU | both hold |
| Embedding runner <= 1.0 GB RAM on CPU | USS of the runner serving the embedder (S7, S8) | <= 1.0 GB |
| Backend <= 0.6 GB | backend process USS | <= 0.6 GB |
| Ingestion worker <= 0.4 GB peak | worker USS during S7 | <= 0.4 GB |
| PostgreSQL <= 0.4 GB | n/a | not measured (not installed) |
| Envelope: 1 running, <= 4 queued, beyond that a clear refusal | S5 | 3 complete; overflow refused with `queue_full` |
| Envelope: 1 generation model + 1 embedder resident | `/api/ps` in every sample, S10 included | never two generation models |
| Envelope: context 8192 default, 16384 where measured to fit | S2 at 16384: full GPU placement (`size_vram == size`), no shared-memory growth | holds |
| Envelope: one ingestion job, batches <= 16, file <= 50 MB, PDF <= 500 pages | S7 and failure injection F4 | holds |
| Envelope: <= 100,000 chunks | Gate D measurement (`experiments/results/gate-d/scale.json`), not re-run | cited |
| Envelope: <= 2 optional processes | S4 (two running) plus a third refused (existing test) | holds |

A missed target is recorded with the failing workload and numbers and the 3.9 mitigation order; targets are never
changed quietly.

## 8. Thresholds, backpressure and recovery (section 3.7)

Behaviour under test (implemented in Gate F as the resource governor):

- **warn** (XODUS USS + shared GPU >= 3.6 GB, or XODUS VRAM >= 4.2 GB): ingestion held (worker stopped, job requeued,
  resumes from its checkpoint), idle optional processes stopped, new optional processes refused.
- **stop** (>= 5.5 GB, or >= 4.8 GB VRAM, or system available memory < 1.5 GB): as warn, plus new heavy work refused with
  a clear message (tasks, ingestion requests, model switches, knowledge search or ask when the embedder is not already
  loaded); chat stays available.
- **recover**: after 30 s continuously below the current level the governor steps down; on reaching ok it releases
  ingestion and writes the episode (start, end, peak values, actions) to the log and the audit log.
- Never kills a user process; never claims to prevent Windows running out of memory.

Tests: unit tests on recorded snapshots (every transition and action); live runs with lowered thresholds set by
environment variables (stated in the result file); one bounded live ballast run for the available-memory floor.

## 9. Failure injection (live, real stack)

| # | Failure | Injected by | Expected |
|---|---|---|---|
| F1 | Ollama down | `stop-ollama.ps1` while the backend runs | chat answers a clear `provider_unavailable`-style error, backend stays up; works again after `start-ollama.ps1` |
| F2 | Database unavailable | another process holds the SQLite write lock for longer than the busy timeout | writes fail with a clear message, reads keep working, nothing corrupted (`PRAGMA integrity_check`) |
| F3 | Out of VRAM | a second Ollama server on 127.0.0.1:11435 holding the installed `qwen3:4b-instruct` with a large context | observed behaviour recorded (offload, spill, refusal); no crash; numbers reported |
| F4 | Oversized file | a 51 MB text file and a 501-page PDF in the corpus root | refused with `file_too_large` / page-limit reason, nothing stored |
| F5 | Malformed manifest | a broken plugin manifest in an isolated plugins folder | listed in `/api/capabilities/rejected` with its reason; backend starts |
| F6 | Timeout | fixture plugin in `slow` mode with a 2 s timeout | `timeout` error, process stopped |
| F7 | Cancellation mid-stream | S6, and a task cancelled while streaming | consistent final state, no orphan process |
| F8 | Plugin crash | fixture plugin in `crash` mode | clear `crashed` error, next call restarts it |

After every failure: audit chain verified (`/api/audit/verify`), `PRAGMA integrity_check` on the run's database, and no
XODUS-launched process left without a parent.

## 10. Security rows completed in Gate F

- Per-install API token: requests without it, with a wrong one, from another origin, with a foreign `Host` are refused;
  the backend is not reachable on a non-loopback interface (probe of the machine's own LAN address).
- Canary secret scan: a canary secret configured for a plugin never appears in logs, prompts sent to the model, the
  database, the audit log or experiment output.

## Amendments

All three were made on 29 Oct 2026 after the harness dry run (`experiments/results/gate-f-dry-20261029-025857`, one
short run, labelled dry, not used for any decision) and **before** the smoke benchmark and the measured runs.

1. **Exited processes and empty samples (harness).** A process that exits between being listed and being read
   (`NoSuchProcess`) is dropped from that sample: it holds no memory, and it is not an unreadable value. A sample taken
   before any XODUS process exists counts as complete. Reason: the dry run's only "incomplete" samples (S1 74%, S10 95%)
   were exactly these two artefacts: 2 reads of short-lived Ollama runners started without a model (device and memory
   probes) and the empty samples before startup. The 98% rule stays as written for values that cannot be read
   (`AccessDenied`).
2. **The governor's RAM check is USS only; shared GPU memory is reported apart.** Section 3 said the addition would be
   checked, not assumed. Measured: the generation runner's shared GPU memory stayed at 634 MiB while `/api/ps` showed the
   model fully on the GPU, and in every steady window (S3, S4, S8, S9) USS + shared GPU memory **exceeded** the
   system-wide increase over the quiet idle baseline by about 300 MiB (for example S8: USS 3,001 MiB, + shared 3,635 MiB,
   system increase 3,318 to 3,330 MiB), while USS alone was within 12.5% of it. Adding it counts memory twice, and in the
   dry run it alone moved the normal ingestion workload (USS 3.0 GB) over the 3.6 GB warning level. Shared GPU memory is
   still measured, reported per scenario and shown next to the check.
3. **Holding ingestion also unloads the embedding model.** Measured: once the governor held ingestion, the CPU embedder
   (928 MiB USS) stayed resident for its 10-minute keep-alive, so the level that caused the hold could not fall and
   ingestion would stall for about 10 minutes at a time. The governor now asks Ollama to unload the embedder when it
   holds ingestion; a later search loads it again on demand (and, at the stop level, a search that would have to load it
   is refused, as section 8 already says).

4. **Mitigation adopted after the first measured runs (31 Oct 2026; section 3.9 of the brief).** Runs A and B
   (`experiments/results/gate-f-20261029-032959`) missed the RAM goal: peak summed USS 5.04 GB in S9. Root cause, from
   Ollama's own log: Ollama 0.40.11 runs the model in llama.cpp's `llama-server`, whose host-RAM prompt cache (default limit
   8192 MiB) kept every earlier prompt (18 prompts, 2,718 MiB at the end of S9). `scripts/cache_experiment.py`
   (`experiments/results/gate-f-cache-20261029-210400`; one pass per condition, 20 retrieval questions and a 6-turn chat)
   measured the generation runner's USS: default 1,026 to 5,352 MiB and still rising; `LLAMA_ARG_CACHE_RAM=0` 1,026 to
   1,204 MiB; with `LLAMA_ARG_CTX_CHECKPOINTS=2` as well, 1,026 to 1,082 MiB and flat. Median time to first token showed no
   measurable cost (questions 1,294 / 1,159 / 1,289 ms; chat turns 205 / 178 / 164 ms). `scripts/start-ollama.ps1` now
   sets both. No target or threshold changed. Runs A and B stay as the record of the unmitigated state; the measured
   runs are repeated on this configuration and those are the budget of record. Not measured: conversations longer than
   6 turns, where fewer checkpoints may mean more prompt re-processing (history is bounded at 3,000 tokens).

The dry run also exposed a data-consistency bug, fixed before the measured runs: cancelling a job that had been put back
in the queue (by a pause, a resource hold or a worker crash) left its partial document in the `ingesting` state for ever.
Regression test: `test_ingest_pipeline.py::test_cancelling_an_interrupted_job_while_it_is_queued_removes_its_partial_document`.
