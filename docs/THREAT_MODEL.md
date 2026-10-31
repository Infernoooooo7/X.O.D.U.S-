# XODUS threat model

Status: Gate F (29 Oct 2026; the cross-origin, secrets and resource-exhaustion rows and residual risks 3 and 21 updated, Gate F
residual risks added; Gate E added the MCP, UI, UI-action and Ollama cloud-guard rows). Every row keeps two things apart:
**control implemented** (code exists) and **property tested** (a test that tries to break it ran and passed).
Prompt wording and a model's refusals are never counted as a control.

## Assets

- The user's files inside configured roots (`[[permissions.roots]]`), and everything outside them.
- XODUS's own data folder (`data/`): the audit log, plugin work folders.
- Secrets handed to plugins (only via `XODUS_SECRET_<NAME>` and an explicit `plugins.approved_secrets` entry).
- The machine's RAM, VRAM and processes.

## Trust boundaries

| Boundary | Trusted side | Untrusted side |
|---|---|---|
| Browser to backend (HTTP on 127.0.0.1) | backend | any web page the user has open; any local process |
| Backend to Ollama (HTTP on 127.0.0.1) | backend | model output (text, tool calls, plans) |
| Backend to capabilities | StepGate, first-party in-process code | capability output; plugin processes and their messages |
| Files, file names, tool results | nothing | all of it |
| Configuration (`config/*.toml`, env) | the user | n/a (the user owns it) |

## Attackers considered

1. **Malicious content:** a document, file name or tool result containing instructions (prompt injection).
2. **A compromised or confused model:** emits unknown tools, extra arguments, path escapes, oversized plans.
3. **A malicious or buggy plugin:** crashes, hangs, floods output, lies in error messages, spawns processes.
4. **A web page in the user's browser:** reaches loopback ports (CSRF, DNS rebinding).

Out of scope: an attacker who already runs code as the user (they can read the files, edit the config and
the audit log directly), physical access, and the operating system itself.

## Threats, controls and tests

| Threat | Control implemented | Property tested | Status |
|---|---|---|---|
| Prompt injection via documents, file names, tool output | The capability set offered to a task is fixed before any untrusted content is read and can only narrow. After a task has read untrusted content, any step with a side effect needs approval. Plan-then-execute: the planner never sees tool output; a re-plan sees only XODUS-generated error codes (an out-of-process plugin's message text is dropped). Tool output is wrapped and labelled as data (prompt-level only). | `test_executor.py::test_injection_corpus_produces_no_unapproved_effects` (9 attacks played by a scripted model that obeys every injection: zero unapproved executions, only the legitimate read appears in the audit log). `test_plugins.py::test_plugin_error_text_never_reaches_the_planner`, `test_react_gets_plugin_errors_but_cannot_act_on_them`. One live run with the real model (`test_ollama_live.py::test_live_injection_attempt_produces_no_unapproved_write`). | Tested. The full injection corpus of the research task suite is Gate G. |
| Capability or permission escalation | One `StepGate` for every call path (task steps, plan validation, direct invocation). It checks: capability exists, was offered, is available, every declared permission is granted, arguments validate against a schema that forbids undeclared fields. Grants come only from config, from a closed vocabulary. Budgets are fixed from config at task start. | `test_gate.py` (unknown, malformed, unoffered and ungranted capabilities; smuggled `root`/`permissions`/`requires_approval` arguments; non-object arguments). `test_config.py::test_global_grants_vocabulary_is_closed`. `test_tasks_api.py::test_task_limits_and_validation` (no budget override). `test_executor.py` plan step cap. | Tested |
| Path traversal, links, Windows path tricks | `security/paths.py`: relative paths only. It rejects `..`, drive letters, rooted/UNC/device paths (`\\?\`, `\\.\`), `:` (alternate data streams), wildcards and control characters, trailing dot or space, reserved device names (also `nul.txt`, `COM¹`), 8.3 short names, and any symlink or junction on the way. The resolved path must stay inside the resolved root. Checked by the gate before the capability runs, again inside the capability, and again after an approval. | `test_paths.py`, `test_list_files.py` (23 escape cases), `test_gate.py`. Real junctions created with `mklink /J`; real symlink created (Developer Mode is on here). `test_executor.py::test_approval_does_not_survive_a_change_to_the_target` swaps a folder for a junction after approval. | Tested |
| Approval bypass and unintended side effects | An approval is bound to SHA-256(task, step, capability id and version, canonical arguments, resolved targets). Approving needs that exact hash; executing re-evaluates the step and consumes the approval with the hash of what is about to run. Approvals are single-use, expire (default 600 s), are withdrawn on cancel, and every decision or refusal is audited. Writes go to a temporary file in the same folder, then an atomic rename. The direct-invoke endpoint refuses side-effecting capabilities. | `test_approvals.py` (replay, double approve, wrong hash, modified arguments, other task or step, expiry before and after approval, deny, withdraw). `test_tasks_api.py::test_approval_flow_over_http`. `test_executor.py` (deny, expiry, wait not charged to the wall-time budget). `test_paths.py` and `test_executor.py::test_cancel_during_write_leaves_no_partial_file` (no partial file, no temporary file left). | Tested |
| Malformed manifests, oversized plugin output | Pydantic and JSON-Schema validation on load; unknown fields rejected; remote `$ref` banned; 64 KB manifest cap; 32 KB schema cap; rejected manifests listed with reasons (`GET /api/capabilities/rejected`). Plugin output is read with a byte cap; on overflow the output is discarded and the plugin's process tree killed. Replies must be JSON with the matching id. | `test_manifests.py` (35 invalid manifests, each with its reason, including ids whose tool names could collide; oversized, non-UTF-8 and deeply nested files; 1,500 fuzzed manifests: no crash, every rejection explained). `test_plugins.py` (2 MB output, non-JSON reply, wrong reply id). | Tested |
| Timeouts and cancellation races | Cancellation is checked at every await; in-process code gets a cancel flag; out-of-process plugins are killed through their Job Object (whole tree); pending approvals are withdrawn; every terminal state is recorded with the same final event. | `test_executor.py`: cancel while awaiting approval, during an in-process tool, mid-write, mid-stream. `test_plugins.py::test_cancel_kills_plugin_and_its_grandchild`, `test_timeout_kills_the_plugin`, `test_crash_is_reported_and_the_plugin_restarts`. | Tested |
| Orphaned or escaping processes | Each launched process starts suspended, is put in its own Job Object (kill-on-close, die-on-unhandled-exception, optional memory limit), then resumed. Stopping is always via the job, never via a bare PID; records carry the process creation time. Idle and orphaned trees are reaped. At most 2 optional processes at once. | `test_supervisor.py`: hard kill (TerminateProcess) of a host process and of a real `python -m xodus` backend takes the child and grandchild with it; a `CREATE_BREAKAWAY_FROM_JOB` attempt stays inside the XODUS job; orphan clean-up; idle reaping; PID-reuse guard; process limit. `test_plugins.py::test_job_memory_limit_stops_runaway_allocation`. | Tested |
| Cross-origin and local-network attacks | Binds 127.0.0.1 only (config rejects anything else). `Host` header allow-list (DNS rebinding). State-changing requests carrying a foreign `Origin` or a cross-site `Sec-Fetch-Site` are refused. JSON bodies only. No CORS headers are sent, so other origins cannot read responses. **Per-install API token (Gate F)** on every `/api` request, compared in constant time: scripts send `X-XODUS-Token`; the UI's own pages set it as an `HttpOnly`, `SameSite=Strict` cookie, so page scripts never see it and the browser does not send it with cross-site requests. | `test_config.py` (non-loopback hosts rejected). `test_api.py::test_foreign_host_header_is_refused`. `test_tasks_api.py::test_cross_origin_state_changes_are_refused`. `test_api_token.py` (4): seven routes refuse a missing, wrong or truncated token and nothing runs; the cookie's attributes, the token never written into the page, the cookie alone works and a wrong cookie fails; a valid token does not get past the Origin, `Sec-Fetch-Site` or `Host` checks; one token per install, kept across restarts, a damaged token file replaced. The browser tests pass with the token in place (desktop and 360 px). | Tested. A process running as the same Windows user can still read `data/api_token` or fetch a UI page (residual risk 3) |
| Secrets leaking | Plugins get a scrubbed environment (no `XODUS_*`, no profile paths); a secret reaches a plugin only if `plugins.approved_secrets` lists it. The audit log redacts secret-looking keys. **Gate F:** every approved secret's value is registered when it is resolved (`security/secrets.py`) and replaced by `[redacted]` in tool output shown to a model, in `logs/xodus.jsonl`, `logs/tasks.jsonl` and the audit log. Before this fix the audit log's value redaction was never given any value, and a plugin that echoed its secret put it into the model's prompt and the task log (found by the canary test). | `test_plugins.py::test_environment_is_scrubbed_and_only_approved_secrets_pass` (canary absent). `test_audit.py::test_secrets_are_redacted`. `test_canary.py`: a plugin approved to receive a canary echoes it; a task calls it through the real app; every prompt sent to the model, every log, the audit log and the raw database files are scanned: the canary appears nowhere (and the test fails when the scrub in `result_text` is removed). | Tested for these paths. A plugin's raw output still reaches the user who invokes it directly (it is theirs); Windows Credential Manager (`keyring`) is not used |
| Incomplete deletion | Deleting a document removes its chunks, keyword-index entries, vectors and derived memory facts in **one transaction** (foreign-key cascade plus explicit vector deletes); the database runs with `PRAGMA secure_delete` **and** FTS5 `secure-delete` on, then checkpoints the write-ahead log. The API returns a verification (row counts, index consistency) with every deletion and writes `document_deleted` to the audit log. Ids are never reused (`AUTOINCREMENT`), so an old `[chunk:12]` can never later mean different text. | `test_deletion.py`: row counts and FTS integrity after deletion; **a scan of the raw `.db` and `-wal` files finds neither the text nor the vector bytes of the deleted document**; a four-way control run shows the scan does find residue when either protection is off (measured: neither protection alone is enough; vectors are cleared by sqlite-vec itself); derived facts and collections cascade; other documents untouched; ids not reused. | Tested |
| Malicious documents: injected instructions | Document text is stored as data and never interpreted. In the answer flow the model gets the evidence inside an untrusted-data wrapper (labels sanitised, closing tags defused) and **no tools at all**; citations are validated against the evidence actually shown. A model-invoked search (`kb.search`) goes through the StepGate like any capability, its output taints the task, and any later side effect needs approval. | `test_ingest_security.py` (7 injection documents stored verbatim, control and bidi characters stripped, nothing executed), `test_citations.py` (compromised model cites chunks outside its context: stripped and reported; poisoned passage reaches the model only inside the wrapper; the model call carries no tools), `test_ingest_security.py` compromised-model task (write attempts after a poisoned search: zero executed). Live, real model, one sample: the injected "reply HACKED" was not obeyed (`test_kb_live.py`). | Tested. A model can still be *talked into a wrong answer* by a poisoned passage; only actions are prevented |
| Malicious documents: archive and XML bombs, parser abuse | Size limit enforced on bytes actually read; PDF page limit; DOCX archives are checked for entry count, total uncompressed size and compression ratio **before** anything is read; documents declaring a DTD or entities are refused; XML is parsed as a stream; CSV field-size limit; NUL bytes mean "binary, refused"; a hung parser is ended by the worker's watchdog and the file is retried a bounded number of times, then failed. | `test_parsers.py`, `test_ingest_security.py` (60 MB zip bomb refused with under 4 MiB of memory; entity-expansion documents refused; damaged and fuzzed PDFs raise only parse errors; a 3 MB CSV cell refused; watchdog exit and attempt limit in `test_ingest_worker.py`). | Tested for the formats above. pypdf and the XML parser are third-party code running in the worker process with the user's rights (see residual risks) |
| Path traversal and links through ingestion | The request is validated against the configured root, then **the worker validates again** from the queue entry (links, ADS, device names, `..`), so a file swapped for a junction after queueing is refused. Folder ingestion lists links as skipped and never follows them. | `test_ingest_pipeline.py::test_worker_revalidates_paths_whatever_the_queue_says`, `test_a_file_replaced_by_a_link_after_queueing_is_refused` (real junction), `test_ingest_security.py` (26 hostile paths through the API queue nothing), `test_knowledge_api.py::test_folder_ingest_skips_links_temp_files_and_unsupported_types`. | Tested |
| FTS and SQL injection through queries, names, content | Queries are reduced to quoted word tokens (no FTS5 operators survive); all SQL is parameterised; collection names match a fixed pattern. | `test_retrieval.py` (22 hostile queries: always plain quoted words, never a syntax error), `test_ingest_security.py` (a file named `o'brien; DROP TABLE chunks;--.md`, injection texts: schema intact). | Tested |
| Resource exhaustion by ingestion | Envelope limits: 50 MB per file, 500 PDF pages, 100,000 chunks, one job at a time, embedding batches of at most 16, at most 200 files per request, one active job per file (a unique index in the database). Streaming parsers. The worker runs in a Job Object with a memory limit, at below-normal priority, with a watchdog. | `test_ingest_security.py` (8.6 MB text file and a 150,000-row CSV ingested with under 16 MiB of Python memory; concurrent duplicate requests queue exactly one job), `test_ingest_pipeline.py` (limits, corpus limit leaves no partial chunks), `test_knowledge_api.py` (worker killed: restarted, job resumes). | Tested. Behaviour under real *system* memory pressure is Gate F |
| Cross-collection leakage | Model-invoked search is deny-by-default: only collections named in `[[permissions.collections]]` are searchable, the capability's `collection` argument is an enum of exactly those, and retrieval filters by collection inside the nearest-neighbour search and again by document status. | `test_retrieval.py` (a collection you may not read never appears in either search), `test_citations.py`, `test_ingest_security.py` (a task cannot reach an ungranted collection by naming it, adding arguments or passing a list). | Tested. The user's own API calls are not restricted by these grants |
| Malicious or changing MCP server (Gate E) | Each MCP tool is declared in a reviewed manifest (permissions, side effects, approval, schemas) and passes the same StepGate as any capability. **Tool definitions are pinned**: on every (re)connection the advertised input schema is compared with the manifest and a changed or vanished tool is disabled. The model sees only the manifest's description. **Server-to-client requests are refused** (`sampling/createMessage`, `roots/list`, ...): a server cannot use XODUS's model. No client capabilities are offered. Arguments are checked before the server sees them; output byte cap, timeouts (including during the handshake), protocol checks (JSON-RPC 2.0, matching ids, notification flood limit), crash-loop pause; optional tier, Job Object, scrubbed environment, idle stop, at most 2 optional processes. MCP tools cannot declare file-system permissions. | `test_mcp.py` (38): manifest validation (12 invalid forms); schema drift disables only the changed tool and the server never sees a call to it; a vanished tool is refused by the gate; a sampling request is refused with -32601; scrubbed environment (canary secret absent); hang, crash, 2 MB output, garbage, wrong id, 2,000-notification flood, tool error, bad protocol version, no tools, slow handshake: each fails with a clear code and the process is stopped; a call cancelled during the handshake leaves no process; three failures pause the server; idle stop and restart; process limit refused as `process_limit`; unapproved secret rejected; a task uses an MCP tool through the unchanged orchestrator and its output is wrapped as untrusted. | Tested with first-party fixture servers. Interoperability with third-party MCP servers or SDKs is **not verified** (none was installed) |
| Malicious content in the UI (Gate E) | Model output, document text, file names and audit records are rendered as text by React (escaped); no model or document content is ever inserted as HTML. Citations become buttons only for numeric ids matching the backend's pattern; the cited passage is fetched from the backend, never taken from the answer. The only raw-HTML insertion is a constant theme script in the page source. The UI cannot change configuration, grants or roots (no such endpoint exists). | `test_ui_e2e.py::test_model_output_that_looks_like_html_is_shown_as_text` (desktop and 360 px): a model reply and a user message containing `<img onerror>`, `<script>`, a `javascript:` link and `[chunk:١٢]` are shown literally in the live view and again from the saved conversation; no script runs, no element is created, no citation button appears. Vitest (`lib.test.ts`, `components.test.tsx`) for citation parsing and rendering. | Tested for these rendering paths; no broader XSS fuzzing of the UI |
| Data leaving the machine through Ollama: cloud or remote models, a misconfigured Ollama server (Gate E) | Configuration refuses Ollama cloud model names (a `-cloud` tag) for the generator, the embedder and every profile. The adapter refuses any model Ollama marks as running elsewhere (`remote_model` / `remote_host` in `/api/tags` or `/api/show`, or a `-cloud` tag, which also catches a local alias made with `ollama cp`) **before** any chat, load or embed request is sent; the active model is probed again whenever its name changes. A profile switch to such a model is refused before the current model is unloaded. The ingestion worker checks its embedding model once per worker, so document text is never sent to a remote embedder; that failure is not retried. `/api/health` reads the running Ollama server itself (the process listening on the Ollama port, via psutil): listening on loopback only, `OLLAMA_NO_CLOUD` true, `OLLAMA_REMOTES` unset, no `*` in `OLLAMA_ORIGINS`; the UI shows a warning naming each failed check and the fix. | `test_config.py` (cloud names refused, look-alike local names accepted; a mutation run with the rule disabled made the 5 refusal cases fail). `test_provider.py` (9 entry shapes; health; generate, stream, structured, tools and load refused with zero requests to `/api/chat` or `/api/generate`, whether `/api/tags` or only `/api/show` marks the model; a renamed model is re-probed; embedding refused with zero `/api/embed` requests). `test_models.py` (switch refused as `remote_model`, nothing unloaded). `test_cloud_guard.py` (27: every check and value, unreadable environment is `unknown`, nothing listening is `unknown`, the real environment of the process serving the fake Ollama read through `/api/health`, startup embedder status, the worker's embedder sends no text, the ingest error is not retried). `test_ui_e2e.py` (desktop and 360 px): the warning appears, passes axe, and disappears once fixed; a remote model is named and chat sends nothing. Live: the running Ollama 0.40.11 (started by `scripts\start-ollama.ps1`) passed all four checks; both installed models carry no remote fields. | Tested against fakes and the live server's settings. **Not verified:** what Ollama itself does with `OLLAMA_NO_CLOUD=1` (no request to a cloud model was attempted, since that would contact ollama.com); whether Ollama 0.40.11 puts the remote fields in `/api/show` (both responses are checked) |
| A web page in the browser drives the UI's actions (Gate E) | Every state-changing route keeps the same-origin rule (foreign `Origin` or cross-site `Sec-Fetch-Site` refused), including the new ones (switch model, stop process, pause ingestion, delete conversation). The Ctrl+K palette adds no route: its actions (pause or resume ingestion, verify the audit log) call the same endpoints under the same rule. The static file server maps Next.js segment requests (`__next.<view>.__PAGE__.txt`) to the exported files through the same confined lookup. Stopping a process is limited to optional-tier processes XODUS launched; core processes and anything else are refused. | `test_conversations.py::test_conversation_api_and_audit` and `test_workspace_api.py::test_stopping_an_optional_process_and_refusing_a_core_one` (cross-origin refused); the same-origin rule also made the browser tests fail until they served on the configured port. `test_workspace_api.py::test_segment_prefetch_files_are_found_and_stay_inside_the_ui_folder` (6 escape and missing-file probes answer 404; a file outside the UI folder is never served). | Partly: the API token is still missing (see residual risk 3) |

## Audit log

`data/audit.jsonl` is append-only in code and hash-chained: each record holds the SHA-256 of the
previous one, so edited, deleted or reordered records are detected (`GET /api/audit/verify`,
`test_audit.py`, including a record re-hashed by an attacker). It records capability requests, calls,
results, blocks and cancellations; approval requests, decisions, refusals and use; task start and end;
model load and unload; process start and stop.

## Residual risks (not mitigated, or only partly)

1. **Process isolation is not a sandbox.** Plugins and Job Objects bound lifetime and memory only.
   Plugin code runs with the user's full rights: files, registry, network. `net.none` is a declaration,
   **not enforced** (enforcing it would need firewall changes, which XODUS does not make). The permission
   checks constrain what a *model* can ask a plugin to do, not what plugin code does by itself. Install
   only plugins you trust.
2. **Small models are easy to inject.** The controls stop injected content from causing unapproved
   effects or new capabilities, but a model can still be steered into a wrong or misleading *answer* from
   content it read.
3. **The API token stops browsers, not the user's own processes.** Since Gate F every `/api` request needs the
   per-install token, but any process running as the same Windows user can read `data/api_token` (or fetch a UI page,
   which sets the cookie) and then drive the API, including approving requests. Such a process is already outside the
   threat model (it can read the data and edit the configuration). Cookies are scoped to the host, not the port: a
   different local web server on 127.0.0.1 receives the cookie too, but its pages are still refused by the Origin and
   `Sec-Fetch-Site` checks for state changes and cannot read responses.
4. **Audit log is tamper-evident, not tamper-proof.** Anyone with the user's file rights can rewrite it.
   Deleting the newest records leaves a valid shorter chain; the head hash written to `logs/xodus.jsonl` at
   startup is only a weak anchor.
5. **Time-of-check to time-of-use.** Paths are re-checked just before use and atomic writes stay in the
   checked folder, but a junction swapped in during the last milliseconds is not fully excluded.
6. **In-process cancellation is cooperative.** Python threads cannot be killed; first-party code checks
   its cancel flag. Only first-party code runs in-process.
7. **Approvals live in memory.** A backend restart drops pending approvals; their tasks die with the
   backend anyway.
8. **Plugin manifests can contain expensive regular expressions** (`pattern`) that slow argument
   validation. Plugins are installed by the user; there is no regex timeout.
9. **Job memory accounting.** `PeakJobMemoryUsed` was measured to include refused commit attempts, so it
   is not used as a measurement. The limit itself was verified to be enforced.
10. **Windows-only.** Job Objects, performance counters and path rules are implemented for Windows.

Added in Gate D:

11. **Third-party parsers run with the user's rights.** pypdf and Python's XML parser read untrusted files in
    the ingestion worker. The worker is a separate process in a Job Object (memory limit, killed with the
    backend, watchdog), so a parser crash or hang costs one file, but it is **not a sandbox**: a memory-safety
    or logic flaw in a parser could run code as the user. Keep dependencies updated; ingest only files you
    would be willing to open.
12. **Deletion covers the database, not copies of it.** The raw-file scan proves the text and vectors leave
    `xodus.db` and its `-wal` file. It cannot reach: the original source file (XODUS never deletes it),
    SSD wear-levelling or file-system journal remnants, Windows shadow copies and backups, the
    OS paging file, and text that was copied into a **conversation, a note or an exported memory file**.
    A tool that opens the database without XODUS's connection settings (for example the `sqlite3` shell) does
    not apply `secure_delete` and can leave residue when it deletes.
13. **`retrieval_log` and job rows keep metadata.** Queries are stored only as a hash and length, but result
    chunk ids, timings and file names (in `ingestion_jobs`) remain after a document is deleted. Chunk ids are
    never reused, so the ids point at nothing.
14. **Model-visible retrieval is only as private as the grant.** A document in a granted collection can be
    read by any model-invoked task that has the grant, including text a poisoned document is trying to
    exfiltrate through the *answer*. Actions are controlled; what a model may *say* about granted text is not.
15. **Insufficient-evidence detection is heuristic.** It is a term-overlap test (and an optional vector
    threshold), calibrated on 30 queries over one corpus. It can wrongly say "insufficient" or wrongly accept a
    weak match; treat it as a convenience, not a guarantee. The answer itself is not fact-checked: the validator
    proves a citation points at a passage that was shown to the model, **not that the passage supports the claim**.
16. **Several writers, one hash-chained audit log.** Only the backend appends to `data/audit.jsonl`; the worker's
    outcomes are in the `ingestion_jobs` table and `logs/ingestion-worker.jsonl`. Two writers would corrupt the
    chain, so this separation is deliberate and must be kept.
17. **Embeddings leak information.** Vectors can be partly inverted to recover text. They are stored locally
    and removed with their document, but a copy of `xodus.db` contains them.

Added in Gate E:

18. **MCP pins compare input schemas only.** A server could keep a tool's schema and change what the tool *does*.
    Pinning catches a changed or vanished definition, not changed behaviour: read a server's code before installing it,
    as for plugins. Third-party MCP servers have not been tested with XODUS's client.
19. **The UI trusts the backend completely.** Anyone who can reach the API (any local process, residual risk 3) can do
    everything the UI can, including approving actions, switching models and deleting conversations.
20. **Conversations are stored in plain text** in `xodus.db`, like documents. Deleting one removes it from the database
    file (secure delete); copies in exports, backups or the OS paging file are out of reach (residual risk 12).
21. **The resource governor (Gate F) measures every 2 s and acts on the next check.** A fast allocation can overshoot a
    level between two checks. It holds, stops and refuses; it never stops a process XODUS did not launch, and it cannot
    stop Windows itself from running out of memory. Its VRAM check counts only XODUS's own processes (user decision): a
    different application filling the GPU does not raise XODUS's level; the effect shows up as slower replies or as
    memory moving to the CPU, which the RAM checks see.
22. **Model profiles are switched in memory.** A restart returns to the default profile; a generation model left
    loaded by an earlier run is unloaded at startup.
23. **The Ollama settings check is a report, not a lock.** It sees Ollama's environment and listening sockets only;
    settings Ollama reads from anywhere else are invisible to it, so "checked" means those four checks passed, not that
    Ollama never contacts the internet. A failed check shows a warning; XODUS keeps working (remote models stay
    refused). The generation model is re-checked at startup and at every switch, the embedding model once per backend
    and once per ingestion worker: replacing a local model with a remote alias of the same name in between
    (`ollama cp`) is caught only at the next check.
