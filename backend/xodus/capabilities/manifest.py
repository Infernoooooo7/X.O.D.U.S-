"""Capability manifests (master prompt 4.2) and their validation.

A manifest that fails validation is rejected with a visible reason; it is never partly loaded.
Input schemas must be JSON Schema objects with additionalProperties: false, so undeclared
arguments (for example a model trying to add "permissions" or "root") are always rejected.
Path arguments are declared with "x-xodus-path": "<fs permission>" on a top-level property and
are canonicalised and confined by the executor before the capability runs.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from xodus.security.policy import parse_permission

# Dot-separated segments of lowercase words joined by single underscores. No "__" and no trailing "_",
# so the tool name shown to a model ("." -> "__") maps back to exactly one capability id.
_SEGMENT = r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*"
CAPABILITY_ID = rf"^{_SEGMENT}(?:\.{_SEGMENT}){{1,3}}$"
PATH_KINDS = {"dir", "file", "new_file"}
MAX_SCHEMA_BYTES = 32_000
_DEPENDENCY = r"^(model|capability):[a-z0-9_.:-]{1,64}$"


def _walk_schema(node: Any, depth: int = 0) -> Any:
    if depth > 32:
        raise ValueError("schema is nested too deeply")
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value, depth
            yield from _walk_schema(value, depth + 1)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_schema(value, depth + 1)


def strip_annotations(schema: Any) -> Any:
    """The schema as shown to a model: XODUS annotations removed."""
    if isinstance(schema, dict):
        return {k: strip_annotations(v) for k, v in schema.items() if not str(k).startswith("x-xodus")}
    if isinstance(schema, list):
        return [strip_annotations(v) for v in schema]
    return schema


class CapabilityManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=CAPABILITY_ID, min_length=3, max_length=64)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=1000)
    version: str = Field(pattern=r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    permissions: tuple[str, ...] = Field(max_length=16)
    dependencies: tuple[str, ...] = Field(default=(), max_length=16)
    execution_mode: Literal["in_process", "subprocess", "mcp"]
    process_tier: Literal["none", "optional"]
    resource_class: Literal["light", "model", "heavy"] = "light"
    timeout_s: float = Field(gt=0, le=300)
    max_output_bytes: int = Field(gt=0, le=1_000_000)
    cancellable: bool = False
    requires_approval: Literal["never", "consequential", "always"]
    side_effects: Literal["none", "local", "external"]
    undoable: bool | None = None

    @field_validator("permissions")
    @classmethod
    def _permissions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        seen: set[str] = set()
        for permission in value:
            parse_permission(permission)
            if permission in seen:
                raise ValueError(f"duplicate permission {permission!r}")
            seen.add(permission)
        if {"net.none", "net.outbound"} <= seen:
            raise ValueError("net.none and net.outbound contradict each other")
        if {"exec.none", "exec.spawn"} <= seen:
            raise ValueError("exec.none and exec.spawn contradict each other")
        return value

    @field_validator("dependencies")
    @classmethod
    def _dependencies(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for dep in value:
            if not isinstance(dep, str) or not re.match(_DEPENDENCY, dep):
                raise ValueError(f"dependency {dep!r} must look like 'model:<capability>' or 'capability:<id>'")
        return value

    @field_validator("input_schema", "output_schema")
    @classmethod
    def _schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value)) > MAX_SCHEMA_BYTES:
            raise ValueError(f"schema is larger than {MAX_SCHEMA_BYTES} bytes")
        for key, ref, _ in _walk_schema(value):
            if key in ("$ref", "$dynamicRef") and not (isinstance(ref, str) and ref.startswith("#")):
                raise ValueError("only local '#/...' references are allowed (no remote $ref)")
        try:
            Draft202012Validator.check_schema(value)
        except SchemaError as exc:
            raise ValueError(f"not a valid JSON Schema: {exc.message}") from None
        return value

    @model_validator(mode="after")
    def _consistency(self) -> CapabilityManifest:
        schema = self.input_schema
        if schema.get("type") != "object":
            raise ValueError("input_schema must have type 'object'")
        if schema.get("additionalProperties") is not False:
            raise ValueError("input_schema must set additionalProperties to false (undeclared arguments are rejected)")
        props = schema.get("properties", {})
        if not isinstance(props, dict):
            raise ValueError("input_schema.properties must be an object")
        fs_permissions = {p for p in self.permissions if p.startswith("fs.")}
        top_level = 0
        for name, prop in props.items():
            if not isinstance(prop, dict) or "x-xodus-path" not in prop:
                continue
            top_level += 1
            permission = prop["x-xodus-path"]
            kind = prop.get("x-xodus-path-kind", "dir")
            if permission not in fs_permissions:
                raise ValueError(f"argument {name!r} is a path for {permission!r}, which the manifest does not declare")
            if prop.get("type") != "string":
                raise ValueError(f"path argument {name!r} must have type 'string'")
            if kind not in PATH_KINDS:
                raise ValueError(f"path argument {name!r} has unknown kind {kind!r} (use one of {sorted(PATH_KINDS)})")
            if kind == "new_file" and not str(permission).startswith("fs.write"):
                raise ValueError(f"path argument {name!r} creates files, which needs an fs.write permission")
        all_annotations = sum(1 for key, _, _ in _walk_schema(schema) if key == "x-xodus-path")
        if all_annotations != top_level:
            raise ValueError("x-xodus-path is only supported on top-level input properties")
        expected_tier = "none" if self.execution_mode == "in_process" else "optional"
        if self.process_tier != expected_tier:
            raise ValueError(f"execution_mode {self.execution_mode!r} requires process_tier {expected_tier!r}")
        if self.side_effects == "external" and self.requires_approval == "never":
            raise ValueError("capabilities with external side effects must require approval")
        if any(p.startswith("fs.write") for p in self.permissions) and self.side_effects == "none":
            raise ValueError("an fs.write permission implies side_effects 'local' or 'external'")
        return self

    def path_arguments(self) -> dict[str, tuple[str, str]]:
        """{argument name: (fs permission, kind)} for declared path arguments."""
        props = self.input_schema.get("properties", {})
        return {
            name: (prop["x-xodus-path"], prop.get("x-xodus-path-kind", "dir"))
            for name, prop in props.items()
            if isinstance(prop, dict) and "x-xodus-path" in prop
        }


def check_secret_names(value: tuple[str, ...]) -> tuple[str, ...]:
    for name in value:
        if not isinstance(name, str) or not re.match(r"^[A-Z][A-Z0-9_]{0,63}$", name):
            raise ValueError(f"secret name {name!r} must be UPPER_SNAKE_CASE")
    return value


class PluginEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command: tuple[str, ...] = Field(min_length=2, max_length=16)

    @field_validator("command")
    @classmethod
    def _command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for part in value:
            if not isinstance(part, str) or not part or len(part) > 200 or "\x00" in part:
                raise ValueError("command parts must be non-empty strings of at most 200 characters")
        if value[0] != "python":
            raise ValueError("only Python plugins are supported: command must start with 'python'")
        if not value[1].endswith(".py"):
            raise ValueError("the second command part must be the plugin's .py script")
        return value


class PluginManifest(CapabilityManifest):
    """manifest.json of an out-of-process plugin in plugins/<name>/."""

    entry: PluginEntry
    secrets: tuple[str, ...] = Field(default=(), max_length=8)

    @field_validator("secrets")
    @classmethod
    def _secrets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return check_secret_names(value)

    @model_validator(mode="after")
    def _out_of_process(self) -> PluginManifest:
        if self.execution_mode == "in_process":
            raise ValueError("plugins run out of process: execution_mode must be 'subprocess'")
        if self.execution_mode == "mcp":
            raise ValueError("MCP tools are declared in an MCP server manifest (\"kind\": \"mcp_server\"), not as a plugin")
        return self


MCP_TOOL_NAME = r"^[A-Za-z0-9_.-]{1,64}$"


class McpToolManifest(CapabilityManifest):
    """One tool of an MCP server, reviewed and pinned by the user. XODUS shows the model this manifest's description
    and validates arguments against this input_schema, never against what the server advertises at run time."""

    mcp_tool: str = Field(pattern=MCP_TOOL_NAME)

    @model_validator(mode="after")
    def _mcp(self) -> McpToolManifest:
        if self.execution_mode != "mcp":
            raise ValueError("tools of an MCP server must have execution_mode 'mcp'")
        if any(p.startswith("fs.") for p in self.permissions):
            raise ValueError("MCP tools cannot declare file-system permissions yet (no way to confine their paths)")
        return self


class McpServerManifest(BaseModel):
    """manifest.json of an MCP server in plugins/<name>/: one stdio process providing several capabilities."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["mcp_server"]
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=1000)
    version: str = Field(pattern=r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")
    entry: PluginEntry
    secrets: tuple[str, ...] = Field(default=(), max_length=8)
    init_timeout_s: float = Field(10.0, gt=0, le=60)
    tools: tuple[McpToolManifest, ...] = Field(min_length=1, max_length=32)

    @field_validator("secrets")
    @classmethod
    def _secrets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return check_secret_names(value)

    @model_validator(mode="after")
    def _unique(self) -> McpServerManifest:
        ids = [t.id for t in self.tools]
        names = [t.mcp_tool for t in self.tools]
        if len(ids) != len(set(ids)):
            raise ValueError("tool capability ids must be unique")
        if len(names) != len(set(names)):
            raise ValueError("each MCP tool may be declared once")
        return self
