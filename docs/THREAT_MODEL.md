# XODUS threat model

Status: Gate C (27 Oct 2026). Every row keeps two things apart:
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
| Cross-origin and local-network attacks | Binds 127.0.0.1 only (config rejects anything else). `Host` header allow-list (DNS rebinding). State-changing requests carrying a foreign `Origin` or a cross-site `Sec-Fetch-Site` are refused. JSON bodies only. No CORS headers are sent, so other origins cannot read responses. | `test_config.py` (non-loopback hosts rejected). `test_api.py::test_foreign_host_header_is_refused`. `test_tasks_api.py::test_cross_origin_state_changes_are_refused`. | Partly. **The per-install API token is not implemented yet**, so any local process running as the user can call the API. |
| Secrets leaking | Plugins get a scrubbed environment (no `XODUS_*`, no profile paths); a secret reaches a plugin only if `plugins.approved_secrets` lists it. The audit log redacts secret-looking keys and known secret values. | `test_plugins.py::test_environment_is_scrubbed_and_only_approved_secrets_pass` (canary absent). `test_audit.py::test_secrets_are_redacted`. | Partly. A canary scan across all logs, prompts and stored data is Gate F; Windows Credential Manager (`keyring`) is not used yet. |
| Incomplete deletion | n/a until Gate D (nothing is stored yet). | none | Not applicable yet |

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
3. **No API token yet.** Any local process running as the user can drive the API, including approving
   requests. Browsers are blocked by the Origin, Host and JSON rules above.
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
