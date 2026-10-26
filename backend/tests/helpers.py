"""Test doubles: a scripted model provider and test-only capabilities (never registered in production)."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from xodus.capabilities.list_files import ListFiles
from xodus.capabilities.manifest import CapabilityManifest
from xodus.capabilities.registry import CapabilityCancelled, CapabilityRegistry, InProcessCapability, InvocationContext
from xodus.orchestrator.budget import ExecutionBudget
from xodus.orchestrator.gate import StepGate
from xodus.orchestrator.runner import ARMS, ExecutorDeps
from xodus.orchestrator.task import Task
from xodus.providers.base import Completion, InvalidStructuredOutput, ModelInfo, StreamChunk, ToolCall, Usage
from xodus.resources.admission import AdmissionGate
from xodus.security.approvals import ApprovalStore
from xodus.security.audit import AuditLog
from xodus.security.paths import atomic_write_bytes, resolve_new_file
from xodus.security.policy import PermissionPolicy, Root


def tool_call(name: str, args: Any) -> Completion:
    return Completion(content="", tool_calls=[ToolCall(name, args)], usage=Usage(20, 5))


class ScriptedProvider:
    """Plays back scripted model behaviour, including a fully compromised model that obeys injections."""

    name = "scripted"

    def __init__(self, *, tool_turns: list[Any] | None = None, plans: list[Any] | None = None,
                 answer: str = "Here is the answer.", stream_delay: float = 0.0,
                 confirmed: set[str] | None = None) -> None:
        self.tool_turns = list(tool_turns or [])
        self.plans = list(plans or [])
        self.answer = answer
        self.stream_delay = stream_delay
        self._confirmed = confirmed if confirmed is not None else {"completion", "structured", "tools"}
        self.calls: list[dict[str, Any]] = []

    @property
    def model(self) -> str:
        return "scripted-model"

    async def health(self) -> dict[str, Any]:
        return {"status": "ok", "model": self.model}

    async def list_models(self) -> list[dict[str, Any]]:
        return []

    async def model_info(self, model: str | None = None) -> ModelInfo:
        caps = sorted(self._confirmed)
        return ModelInfo(self.model, None, 8192, None, None, None, caps, caps, caps)

    async def probe(self) -> ModelInfo:
        return await self.model_info()

    def confirmed_capabilities(self) -> set[str] | None:
        return set(self._confirmed)

    async def chat_with_tools(self, messages, tools, budget, *, seed=None) -> Completion:
        self.calls.append({"kind": "tools", "messages": copy.deepcopy(messages), "tools": [t.name for t in tools]})
        turn = self.tool_turns.pop(0) if self.tool_turns else Completion(self.answer, usage=Usage(10, 5))
        if callable(turn):
            turn = turn(messages)
        if isinstance(turn, Exception):
            raise turn
        return turn

    async def structured(self, messages, schema, budget, *, seed=None):
        self.calls.append({"kind": "structured", "messages": copy.deepcopy(messages), "schema": schema})
        plan = self.plans.pop(0) if self.plans else {"steps": []}
        if callable(plan):
            plan = plan(messages)
        error = next(iter(Draft202012Validator(schema).iter_errors(plan)), None)
        if error is not None:  # the real provider validates too
            raise InvalidStructuredOutput(f"does not match the schema: {error.message}")
        return plan, Completion(json.dumps(plan), usage=Usage(30, 20))

    async def generate(self, messages, budget, *, seed=None) -> Completion:
        self.calls.append({"kind": "generate", "messages": copy.deepcopy(messages)})
        return Completion(self.answer, usage=Usage(10, 5))

    async def stream(self, messages, budget, *, seed=None) -> AsyncIterator[StreamChunk]:
        self.calls.append({"kind": "stream", "messages": copy.deepcopy(messages)})
        for word in self.answer.split():
            if self.stream_delay:
                await asyncio.sleep(self.stream_delay)
            yield StreamChunk(text=word + " ")
        yield StreamChunk(final={"done": True}, usage=Usage(10, len(self.answer.split())))

    async def embed(self, texts, *, timeout_s=60.0, dimensions=None):
        return [[0.0] * 4 for _ in texts]

    async def load(self, *, num_ctx: int) -> None:
        return None

    async def unload(self) -> None:
        return None

    async def running_models(self):
        return []

    async def aclose(self) -> None:
        return None


class WriteNote(InProcessCapability):
    """Test-only side-effecting capability: writes a text file atomically inside its root."""

    def __init__(self, root_id: str, root: Path, chunk_delay_s: float = 0.0) -> None:
        self.root = root.resolve()
        self.executions = 0
        self.chunk_delay_s = chunk_delay_s
        self.manifest = CapabilityManifest(
            id="test.write_note",
            name="Write note (test)",
            description="Write a short text note to a file in the sandbox.",
            version="1.0.0",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "text"],
                "properties": {
                    "path": {"type": "string", "x-xodus-path": f"fs.write:{root_id}", "x-xodus-path-kind": "new_file"},
                    "text": {"type": "string", "maxLength": 100_000},
                },
            },
            output_schema={"type": "object", "required": ["written"], "properties": {"written": {"type": "integer"}}},
            permissions=(f"fs.write:{root_id}", "net.none", "exec.none"),
            execution_mode="in_process",
            process_tier="none",
            timeout_s=10,
            max_output_bytes=1000,
            cancellable=True,
            requires_approval="consequential",
            side_effects="local",
            undoable=False,
        )

    def run(self, args: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
        self.executions += 1
        target = resolve_new_file(self.root, args["path"])
        data = args["text"].encode("utf-8")

        def cancelled() -> bool:
            if self.chunk_delay_s:
                time.sleep(self.chunk_delay_s)
            return ctx.cancel.is_set()

        atomic_write_bytes(target, data, cancelled=cancelled, chunk_size=4)
        return {"written": len(data)}


class SlowCapability(InProcessCapability):
    """Test-only read-only capability that runs for a while and honours cancellation."""

    def __init__(self) -> None:
        self.cancel_seen = False
        self.started = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.manifest = CapabilityManifest(
            id="test.slow",
            name="Slow (test)",
            description="Waits for a few seconds, then returns.",
            version="1.0.0",
            input_schema={"type": "object", "additionalProperties": False,
                          "properties": {"seconds": {"type": "number", "maximum": 30}}},
            output_schema={"type": "object"},
            permissions=("net.none", "exec.none"),
            execution_mode="in_process",
            process_tier="none",
            timeout_s=30,
            max_output_bytes=1000,
            cancellable=True,
            requires_approval="never",
            side_effects="none",
        )

    async def invoke(self, args, ctx):
        self._loop = asyncio.get_running_loop()
        return await super().invoke(args, ctx)

    def run(self, args: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
        assert self._loop is not None
        self._loop.call_soon_threadsafe(self.started.set)
        deadline = time.monotonic() + float(args.get("seconds", 5))
        while time.monotonic() < deadline:
            if ctx.cancel.wait(0.02):
                self.cancel_seen = True
                raise CapabilityCancelled("stopped")
        return {"slept": True}


class FailingCapability(InProcessCapability):
    """Test-only read-only capability that always fails at run time (after passing every check)."""

    def __init__(self) -> None:
        self.executions = 0
        self.manifest = CapabilityManifest(
            id="test.failing", name="Failing (test)", description="Always fails.", version="1.0.0",
            input_schema={"type": "object", "additionalProperties": False, "properties": {}},
            output_schema={"type": "object"}, permissions=("net.none", "exec.none"), execution_mode="in_process",
            process_tier="none", timeout_s=5, max_output_bytes=1000, requires_approval="never", side_effects="none",
        )

    def run(self, args: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
        from xodus.capabilities.registry import CapabilityError

        self.executions += 1
        raise CapabilityError("the service it needs is down")


def default_budget(**overrides: Any) -> ExecutionBudget:
    values = dict(max_steps=10, max_model_calls=12, max_total_tokens=100_000, max_wall_s=60, max_tool_output_bytes=4000,
                  max_replans=1, num_ctx=8192, max_output_tokens=512)
    values.update(overrides)
    return ExecutionBudget(**values)


def make_deps(tmp_path: Path, sandbox: Path, provider: Any, extra: list[Any] | None = None,
              grants: tuple[str, ...] = ("fs.read", "fs.write"), ttl_s: float = 5.0) -> ExecutorDeps:
    policy = PermissionPolicy([Root("sandbox", sandbox.resolve(), frozenset(grants))])
    registry = CapabilityRegistry(policy)
    registry.register(ListFiles("sandbox", sandbox), "builtin")
    for cap in extra or []:
        registry.register(cap, "test")
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    audit = AuditLog(data / "audit.jsonl")
    return ExecutorDeps(provider=provider, registry=registry, gate=StepGate(registry, policy, data),
                        approvals=ApprovalStore(audit, ttl_s=ttl_s), audit=audit, admission=AdmissionGate(1, 4),
                        policy=policy)


def start_task(deps: ExecutorDeps, arm: str, goal: str, budget: ExecutionBudget | None = None,
               offered: frozenset[str] | None = None) -> Task:
    task = Task("t" + str(int(time.monotonic() * 1e6))[-8:], goal, arm,
                offered if offered is not None else frozenset(deps.registry.available_ids()))
    task.runner = asyncio.create_task(ARMS[arm](task, deps, budget or default_budget()).run())
    return task


async def run_task(deps: ExecutorDeps, arm: str, goal: str, **kw: Any) -> Task:
    task = start_task(deps, arm, goal, **kw)
    await asyncio.wait_for(task.runner, timeout=30)
    return task


async def wait_event(task: Task, type_: str, timeout: float = 10.0) -> dict[str, Any]:
    async def _find() -> dict[str, Any]:
        async for event in task.follow():
            if event["type"] == type_:
                return event
        raise AssertionError(f"task finished ({task.status}) without a {type_!r} event")

    return await asyncio.wait_for(_find(), timeout)


def audit_events(deps: ExecutorDeps) -> list[dict[str, Any]]:
    return [json.loads(line) for line in deps.audit.path.read_text(encoding="utf-8").splitlines()]


def events_of(task: Task, type_: str) -> list[dict[str, Any]]:
    return [e for e in task.events if e["type"] == type_]
