# Plugins

Each plugin is a folder here with a `manifest.json` and a Python script. XODUS validates the manifest on
startup. If it is invalid, the plugin is rejected and the reason shows in `GET /api/capabilities/rejected`.
No plugin ships with XODUS. The folder `backend/tests/fixtures/plugins_e2e/spawner` is a test fixture,
not an example to install.

## Contract

The plugin runs as its own process, started on first use, inside a Windows Job Object. It speaks
newline-delimited JSON-RPC 2.0 on stdin/stdout:

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

## This is not a sandbox

A plugin runs with your user account's full rights. The declared permissions limit what the *model* may
ask the plugin to do, not what the plugin's own code can do, and `net.none` is not enforced. Only install
plugins whose code you have read and trust.

## Minimal manifest

```json
{
  "id": "notes.word_count",
  "name": "Word count",
  "description": "Count the words in a text.",
  "version": "1.0.0",
  "input_schema": {"type": "object", "additionalProperties": false, "required": ["text"],
                   "properties": {"text": {"type": "string", "maxLength": 100000}}},
  "output_schema": {"type": "object", "required": ["words"], "properties": {"words": {"type": "integer"}}},
  "permissions": ["net.none", "exec.none"],
  "execution_mode": "subprocess",
  "process_tier": "optional",
  "timeout_s": 5,
  "max_output_bytes": 10000,
  "requires_approval": "never",
  "side_effects": "none",
  "entry": {"command": ["python", "main.py"]}
}
```
