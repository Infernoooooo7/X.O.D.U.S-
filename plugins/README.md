# Plugins and MCP servers

Each folder here is one extension: a `manifest.json` plus a Python script. XODUS validates every manifest at startup.
If it is invalid, the extension is rejected and the reason shows in the Capabilities view and in
`GET /api/extensions` (and `GET /api/capabilities/rejected`). Adding an extension never needs a change to XODUS's
code: drop the folder here and restart the backend.

Two first-party samples ship with XODUS (standard library only, no files, no network):

| Folder | Kind | Capabilities |
|---|---|---|
| `text_stats/` | subprocess plugin | `text.stats`: words, sentences, paragraphs, reading time, frequent words |
| `calc_mcp/` | MCP server (stdio) | `calc.evaluate`: safe arithmetic (an allow-listed syntax tree, never `eval`); `calc.describe`: mean, median, stdev |

The folder `backend/tests/fixtures/` holds test fixtures (some misbehave on purpose); they are not examples to install.

Both kinds run as **optional-tier processes**: started on first use, inside a Windows Job Object with a memory limit
(default 512 MB), stopped after 5 minutes idle, at most 2 at once, and counted in the resource view. On Windows each
one appears as three processes in its job: the venv launcher, the Python interpreter and a `conhost.exe` console host.

## Subprocess plugin contract

The plugin speaks newline-delimited JSON-RPC 2.0 on stdin/stdout:

```
request : {"jsonrpc": "2.0", "id": 1, "method": "invoke",
           "params": {"capability": "<id>", "args": {...}, "roots": {"<root id>": "<absolute path>"}}}
response: {"jsonrpc": "2.0", "id": 1, "result": {...}}
       or {"jsonrpc": "2.0", "id": 1, "error": {"message": "..."}}
```

- **Arguments** are validated against `input_schema` before the plugin sees them. The schema must set
  `"additionalProperties": false`.
- **Path arguments** are declared with `"x-xodus-path": "fs.read:<root>"` (or `fs.write:<root>`) and
  `"x-xodus-path-kind": "dir" | "file" | "new_file"`. XODUS confines them to the root before calling.
- **Output** must be a JSON object that matches `output_schema` and stays under `max_output_bytes`;
  otherwise it is discarded and the process stopped.
- **Environment:** the process gets a scrubbed environment, its own working folder under
  `data/plugins/<id>/`, a timeout, and an optional job memory limit. It receives no secrets unless the
  manifest lists them *and* `plugins.approved_secrets` in config approves them. Values come from
  `XODUS_SECRET_<NAME>`.
- **Approval:** side effects (`"side_effects": "local"` or `"external"`) go through the approval flow.

See `text_stats/manifest.json` for a complete example.

## MCP servers

An MCP server folder has a manifest with `"kind": "mcp_server"` that lists every tool you allow, each with a full
capability manifest (permissions, side effects, approval, input and output schema) plus `"mcp_tool"`, the tool's
name on the server. See `calc_mcp/manifest.json`. XODUS speaks MCP over stdio (protocol versions 2025-06-18,
2025-03-26, 2024-11-05) and implements only `initialize`, `tools/list` and `tools/call`.

What XODUS enforces for MCP servers:

- **Pinned tool definitions.** On every (re)connection XODUS compares each tool's advertised input schema with the
  manifest you reviewed. If the server changes a tool, that tool is disabled until you update the manifest. The model
  only sees the manifest's description, never the server's.
- **No requests from the server.** `sampling/createMessage`, `roots/list`, `elicitation/create` and any other
  server-to-client request is refused: a server cannot use XODUS's model, see your folders or prompt you.
- **No file-system permissions** for MCP tools yet (their paths could not be confined).
- Arguments are validated against the manifest before the server sees them; output is checked against
  `output_schema` and the byte cap; a hung, crashing or garbled server is stopped, and three failures in a minute
  pause it.

Third-party MCP servers are not installed by XODUS. If you add one, read its code first, write the manifest by hand
from its `tools/list` output, and pin only the tools you need.

## This is not a sandbox

A plugin or MCP server runs with your user account's full rights. The declared permissions limit what the *model*
may ask it to do, not what its own code can do, and `net.none` is not enforced. Only install extensions whose code
you have read and trust.
