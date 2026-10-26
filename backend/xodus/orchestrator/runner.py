"""Bounded executor and the two H3 arms (master prompt section 5 and 12).

Both arms share the same enforcement: every step goes through the StepGate, budgets are hard limits,
approvals are bound to the action hash, cancellation is checked at every await, and tool output is
fed back only as truncated, labelled untrusted data.

  ReActRunner (H3 control, baseline B0): the model sees each tool result and decides the next call;
      capped at 10 steps (other budgets are generous safety ceilings).
  PlanExecuteRunner (H3 treatment): the model plans all steps up front from the request alone; the
      plan is validated before anything runs; steps run with fixed arguments; at most one re-plan,
      which sees only XODUS-generated error codes (never tool output); the final answer call sees the
      results but has no tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any, Literal, TypeVar

from jsonschema import Draft202012Validator

from xodus.capabilities.manifest import CapabilityManifest, strip_annotations
from xodus.capabilities.registry import (
    Capability,
    CapabilityError,
    CapabilityRegistry,
    CapabilityTimeout,
    InvalidOutput,
    InvocationContext,
    OutputTooLarge,
)
from xodus.obs import log_event
from xodus.orchestrator.budget import BudgetExceeded, BudgetTracker, ExecutionBudget
from xodus.orchestrator.gate import GateContext, GateDecision, ProposedStep, StepGate
from xodus.orchestrator.prompts import ANSWER_SYSTEM, PLAN_SYSTEM, REACT_SYSTEM
from xodus.orchestrator.task import StepRecord, Task
from xodus.providers.base import Completion, InvalidStructuredOutput, ModelProvider, ProviderError, RequestBudget, ToolSpec
from xodus.resources.admission import AdmissionGate, QueueFull
from xodus.security.approvals import ApprovalError, ApprovalStore, action_hash
from xodus.security.audit import AuditLog
from xodus.security.policy import PermissionPolicy
from xodus.security.untrusted import result_text, wrap_untrusted

T = TypeVar("T")
PLAN_MAX_TOKENS = 768


class TaskBlocked(Exception):
    pass


class PlanFailed(Exception):
    pass


@dataclass
class ExecutorDeps:
    provider: ModelProvider
    registry: CapabilityRegistry
    gate: StepGate
    approvals: ApprovalStore
    audit: AuditLog
    admission: AdmissionGate
    policy: PermissionPolicy
    seed: int | None = None


@dataclass
class StepOutcome:
    status: Literal["ok", "blocked", "failed"]
    code: str
    message: str
    result: dict[str, Any] | None = None
    text: str | None = None


def tool_name(capability_id: str) -> str:
    return capability_id.replace(".", "__")


def roots_for(manifest: CapabilityManifest, policy: PermissionPolicy) -> dict[str, Any]:
    roots = {}
    for permission in manifest.permissions:
        if permission.startswith("fs."):
            root = policy.root_for(permission)
            roots[root.id] = root.path
    return roots


async def invoke_checked(cap: Capability, args: dict[str, Any], ctx: InvocationContext, timeout_s: float) -> tuple[dict[str, Any], int]:
    """Run a capability with a timeout, then check its output against the manifest (schema, byte cap)."""
    manifest = cap.manifest
    try:
        result = await asyncio.wait_for(cap.invoke(args, ctx), timeout=timeout_s)
    except TimeoutError:
        ctx.cancel.set()
        raise CapabilityTimeout(f"{manifest.id} did not finish within {timeout_s:.0f} s.") from None
    if not isinstance(result, dict):
        raise InvalidOutput(f"{manifest.id} returned something other than a JSON object.")
    size = len(json.dumps(result, default=str).encode("utf-8"))
    if size > manifest.max_output_bytes:
        raise OutputTooLarge(f"{manifest.id} returned {size} bytes, over its {manifest.max_output_bytes}-byte cap.")
    error = next(iter(Draft202012Validator(manifest.output_schema).iter_errors(result)), None)
    if error is not None:
        raise InvalidOutput(f"{manifest.id} returned output that does not match its schema: {error.message[:200]}")
    return result, size


class TaskRunner:
    arm = "base"

    def __init__(self, task: Task, deps: ExecutorDeps, budget: ExecutionBudget) -> None:
        self.task = task
        self.deps = deps
        self.tracker = BudgetTracker(budget)
        self.gate_ctx = GateContext(task_id=task.id, allowed=task.offered)

    async def execute(self) -> str:
        raise NotImplementedError

    async def run(self) -> None:
        task = self.task
        task.status = "running"
        task.emit("task_started", arm=self.arm, goal=task.goal, offered=sorted(task.offered),
                  budget=asdict(self.tracker.budget))
        self.deps.audit.append("task_started", task_id=task.id, arm=self.arm, goal=task.goal, offered=sorted(task.offered))
        status, message = "error", None
        try:
            task.answer = await self.execute()
            status = "completed"
        except BudgetExceeded as exc:
            status, message = exc.status, exc.message
        except TaskBlocked as exc:
            status, message = "blocked", str(exc)
        except PlanFailed as exc:
            status, message = "failed_safe", str(exc)
        except ProviderError as exc:
            status, message = "failed_safe", exc.message
        except QueueFull as exc:
            status, message = "failed_safe", str(exc)
        except asyncio.CancelledError:
            status, message = "cancelled", "Cancelled by the user. Nothing further was run."
        except Exception as exc:
            status, message = "error", f"Unexpected error ({type(exc).__name__}); see logs/xodus.jsonl."
            log_event("task_error", logging.ERROR, task_id=task.id, detail=repr(exc))
        finally:
            self.deps.approvals.withdraw_for_task(task.id)
            task.budget = self.tracker.snapshot()
            task.finish(status, message=message, answer=task.answer, budget=task.budget)
            self.deps.audit.append("task_finished", task_id=task.id, status=status, message=message,
                                   steps=self.tracker.steps, model_calls=self.tracker.model_calls, tokens=self.tracker.tokens)

    # ------------------------------------------------------------------ model calls

    async def model_call(self, kind: str, call: Callable[[RequestBudget], Awaitable[T]],
                         max_output_tokens: int | None = None) -> T:
        self.tracker.start_model_call()
        budget = self.tracker.request_budget(max_output_tokens)
        self.task.emit("model_call", kind=kind, n=self.tracker.model_calls)
        async with self.deps.admission.slot():
            result = await call(budget)
        completion = result if isinstance(result, Completion) else result[1]  # structured() returns (data, completion)
        self.tracker.record_usage(completion.usage)
        return result

    # ------------------------------------------------------------------ steps

    def _block(self, step: ProposedStep, record: StepRecord, code: str, message: str) -> StepOutcome:
        record.status, record.code, record.message = "blocked", code, message
        self.deps.audit.append("capability_blocked", task_id=self.task.id, step=step.index,
                               capability=record.capability_id, code=code, message=message)
        self.task.emit("step_blocked", step=step.index, capability=record.capability_id, code=code, message=message)
        return StepOutcome("blocked", code, message)

    def _fail(self, step: ProposedStep, record: StepRecord, code: str, message: str) -> StepOutcome:
        record.status, record.code, record.message = "failed", code, message
        self.deps.audit.append("capability_failed", task_id=self.task.id, step=step.index,
                               capability=record.capability_id, code=code, message=message)
        self.task.emit("step_failed", step=step.index, capability=record.capability_id, code=code, message=message)
        return StepOutcome("failed", code, message)

    async def execute_step(self, step: ProposedStep) -> StepOutcome:
        self.tracker.start_step()  # raises StepLimitReached at the cap
        decision = self.deps.gate.evaluate(step, self.gate_ctx)
        record = StepRecord(index=step.index, capability_id=str(step.capability_id)[:80], args=step.args,
                            origin=step.origin, status="evaluating")
        self.task.steps.append(record)
        self.deps.audit.append("capability_requested", task_id=self.task.id, step=step.index,
                               capability=record.capability_id, args=step.args, origin=step.origin,
                               verdict=decision.verdict, code=decision.code)
        if decision.verdict == "deny":
            return self._block(step, record, decision.code, decision.message)
        if decision.verdict == "approve":
            decision = await self._await_approval(step, decision, record)
        return await self._run(step, decision, record)

    async def _await_approval(self, step: ProposedStep, decision: GateDecision, record: StepRecord) -> GateDecision:
        assert decision.capability is not None and decision.action_hash is not None
        manifest = decision.capability.manifest
        req = self.deps.approvals.request(
            task_id=self.task.id, step_index=step.index, capability_id=manifest.id, capability_name=manifest.name,
            capability_version=manifest.version, args=decision.args, targets=decision.targets,
            side_effects=manifest.side_effects, undoable=manifest.undoable, reasons=decision.reasons,
            hash_=decision.action_hash,
        )
        record.status, record.approval_id = "awaiting_approval", req.id
        self.task.status = "awaiting_approval"
        self.task.emit("approval_required", step=step.index, approval=req.public())
        self.tracker.pause()
        try:
            status = await self.deps.approvals.wait(req.id)
        finally:
            self.tracker.resume()
            if not self.task.terminal:
                self.task.status = "running"
        if status != "approved":
            self._block(step, record, f"approval_{status}", f"{manifest.id} was not approved ({status}); nothing was run.")
            raise TaskBlocked(f"Step {step.index} ({manifest.id}) was not approved ({status}); nothing was run.")
        # Re-check at execution time: files or grants may have changed since the request was shown.
        fresh = self.deps.gate.evaluate(step, self.gate_ctx)
        if fresh.verdict == "deny":
            self._block(step, record, fresh.code, fresh.message)
            raise TaskBlocked(f"Step {step.index} is no longer allowed: {fresh.message}")
        current = action_hash(task_id=self.task.id, step_index=step.index, capability_id=manifest.id,
                              capability_version=manifest.version, args=fresh.args, targets=fresh.targets)
        try:
            self.deps.approvals.consume(req.id, current)
        except ApprovalError as exc:
            self._block(step, record, exc.code, exc.message)
            raise TaskBlocked(f"Step {step.index}: approval check failed ({exc.code}). {exc.message}") from None
        self.task.emit("approval_resolved", step=step.index, approval_id=req.id, status="approved")
        return fresh

    async def _run(self, step: ProposedStep, decision: GateDecision, record: StepRecord) -> StepOutcome:
        cap = decision.capability
        assert cap is not None and decision.args is not None
        manifest = cap.manifest
        ctx = InvocationContext(task_id=self.task.id, step_index=step.index, roots=roots_for(manifest, self.deps.policy))
        record.status = "running"
        self.task.emit("step_started", step=step.index, capability=manifest.id, args=decision.args, targets=decision.targets)
        self.deps.audit.append("capability_call", task_id=self.task.id, step=step.index, capability=manifest.id,
                               args=decision.args, targets=decision.targets)
        started = time.perf_counter()
        timeout = min(manifest.timeout_s, max(0.5, self.tracker.remaining_s()))
        try:
            result, size = await invoke_checked(cap, decision.args, ctx, timeout)
        except CapabilityError as exc:
            record.duration_ms = round((time.perf_counter() - started) * 1000, 1)
            return self._fail(step, record, exc.code, exc.message)
        except asyncio.CancelledError:
            ctx.cancel.set()
            record.status, record.code = "cancelled", "cancelled"
            self.deps.audit.append("capability_cancelled", task_id=self.task.id, step=step.index, capability=manifest.id)
            self.task.emit("step_cancelled", step=step.index, capability=manifest.id)
            raise
        text, truncated = result_text(result, self.tracker.budget.max_tool_output_bytes)
        record.status, record.code = "ok", "ok"
        record.duration_ms = round((time.perf_counter() - started) * 1000, 1)
        record.output_bytes = size
        self.deps.audit.append("capability_result", task_id=self.task.id, step=step.index, capability=manifest.id,
                               bytes=size, duration_ms=record.duration_ms)
        self.task.emit("step_output", step=step.index, capability=manifest.id, preview=text[:600], bytes=size,
                       truncated=truncated, duration_ms=record.duration_ms)
        return StepOutcome("ok", "ok", "ok", result=result, text=text)

    def mark_tainted(self) -> None:
        self.gate_ctx.tainted = True
        self.task.tainted = True

    def manifests(self, ids: frozenset[str]) -> list[CapabilityManifest]:
        out = []
        for cap_id in sorted(ids):
            cap = self.deps.registry.get(cap_id)
            if cap is not None:
                out.append(cap.manifest)
        return out


class ReActRunner(TaskRunner):
    arm = "react"

    async def execute(self) -> str:
        manifests = self.manifests(self.task.offered)
        names = {tool_name(m.id): m.id for m in manifests}
        tools = [ToolSpec(name=tool_name(m.id), description=m.description, parameters=strip_annotations(m.input_schema))
                 for m in manifests]
        messages: list[dict[str, Any]] = [{"role": "system", "content": REACT_SYSTEM},
                                          {"role": "user", "content": self.task.goal}]
        provider, seed = self.deps.provider, self.deps.seed
        index = 0
        while True:
            if tools:
                completion = await self.model_call("react", lambda b: provider.chat_with_tools(messages, tools, b, seed=seed))
            else:
                completion = await self.model_call("answer", lambda b: provider.generate(messages, b, seed=seed))
            if not completion.tool_calls:
                answer = completion.content.strip() or "(The model returned an empty answer.)"
                self.task.emit("answer", text=answer)
                return answer
            messages.append({"role": "assistant", "content": completion.content,
                             "tool_calls": [{"function": {"name": c.name, "arguments": c.arguments}} for c in completion.tool_calls]})
            for call in completion.tool_calls:
                index += 1
                cap_id = names.get(call.name, call.name)
                outcome = await self.execute_step(ProposedStep(index, cap_id, call.arguments, "react"))
                if outcome.status == "ok":
                    self.mark_tainted()
                    content = wrap_untrusted(outcome.text or "", source=str(cap_id), step=index)
                else:
                    content = f"XODUS did not run this call ({outcome.code}): {outcome.message}"
                messages.append({"role": "tool", "tool_name": call.name, "content": content})


def build_plan_schema(manifests: list[CapabilityManifest], max_steps: int) -> dict[str, Any]:
    alternatives = [
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["capability", "args"],
            "properties": {
                "capability": {"const": m.id},
                "args": strip_annotations(m.input_schema),
                "why": {"type": "string", "maxLength": 200},
            },
        }
        for m in manifests
    ]
    item = alternatives[0] if len(alternatives) == 1 else {"anyOf": alternatives}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["steps"],
        "properties": {"steps": {"type": "array", "maxItems": max_steps, "items": item}},
    }


class PlanExecuteRunner(TaskRunner):
    arm = "plan_execute"

    async def _plan(self, manifests: list[CapabilityManifest], feedback: list[str] | None) -> tuple[list[Any], list[str]]:
        catalog = [{"id": m.id, "description": m.description, "input_schema": strip_annotations(m.input_schema)}
                   for m in manifests]
        user = f"Request:\n{self.task.goal}"
        if feedback:
            user += ("\n\nYour previous plan could not be used:\n" + "\n".join(f"- {p}" for p in feedback)
                     + "\nReturn a corrected plan.")
        messages = [{"role": "system", "content": PLAN_SYSTEM + json.dumps(catalog, ensure_ascii=False)},
                    {"role": "user", "content": user}]
        schema = build_plan_schema(manifests, self.tracker.budget.max_steps)
        provider, seed = self.deps.provider, self.deps.seed
        try:
            data, _ = await self.model_call("plan", lambda b: provider.structured(messages, schema, b, seed=seed),
                                            max_output_tokens=PLAN_MAX_TOKENS)
        except InvalidStructuredOutput as exc:
            self.task.emit("plan", steps=None, problem=exc.message)
            return [], [f"the plan was not valid: {exc.message}"]
        steps = data.get("steps", [])
        self.task.emit("plan", steps=steps)
        return steps, self._validate(steps)

    def _validate(self, steps: list[Any]) -> list[str]:
        """Check the whole plan with the StepGate before anything runs."""
        problems = []
        for i, spec in enumerate(steps, 1):
            if not isinstance(spec, dict):
                problems.append(f"step {i}: not an object")
                continue
            decision = self.deps.gate.evaluate(ProposedStep(i, spec.get("capability"), spec.get("args"), "plan"), self.gate_ctx)
            if decision.verdict == "deny":
                problems.append(f"step {i} ({str(spec.get('capability'))[:60]}): {decision.code}: {decision.message[:200]}")
        return problems

    def _feedback(self, index: int, cap_id: str, outcome: StepOutcome) -> str:
        """Re-plan feedback is generated by XODUS. Messages from out-of-process plugins are untrusted, so only their code is passed on."""
        cap = self.deps.registry.get(cap_id)
        trusted = cap is not None and cap.manifest.execution_mode == "in_process"
        detail = f": {outcome.message[:200]}" if trusted else ""
        return f"step {index} ({cap_id}) {outcome.status} with {outcome.code}{detail}"

    async def execute(self) -> str:
        manifests = self.manifests(self.task.offered)
        if not manifests:
            return await self._answer([])
        steps, problems = await self._plan(manifests, None)
        if problems:
            if not self.tracker.start_replan():
                raise PlanFailed("No valid plan: " + "; ".join(problems[:3]))
            self.task.emit("replan", reason="invalid plan", problems=problems)
            steps, problems = await self._plan(manifests, problems)
            if problems:
                raise PlanFailed("No valid plan after one re-plan: " + "; ".join(problems[:3]))
        # From here on the task may only narrow: the planned capabilities are all it can still use.
        self.gate_ctx.allowed = self.gate_ctx.allowed & frozenset(s["capability"] for s in steps)
        results: list[tuple[int, str, str]] = []
        queue = list(steps)
        index = 0
        while queue:
            spec = queue.pop(0)
            index += 1
            outcome = await self.execute_step(ProposedStep(index, spec["capability"], spec.get("args", {}), "plan"))
            if outcome.status == "ok":
                self.mark_tainted()
                results.append((index, spec["capability"], outcome.text or ""))
                continue
            feedback = self._feedback(index, spec["capability"], outcome)
            if not self.tracker.start_replan():
                raise PlanFailed(f"Plan step {feedback}.")
            self.task.emit("replan", reason="step failed", problems=[feedback])
            done = [f"step {i} ({c}) completed" for i, c, _ in results]
            narrowed = self.manifests(self.gate_ctx.allowed)
            if not narrowed:
                raise PlanFailed(f"Plan step {feedback}; no capability is left to re-plan with.")
            queue, problems = await self._plan(narrowed, [feedback, *done, "plan only the remaining work"])
            if problems:
                raise PlanFailed("The re-plan was not valid: " + "; ".join(problems[:3]))
        return await self._answer(results)

    async def _answer(self, results: list[tuple[int, str, str]]) -> str:
        if results:
            body = "\n\n".join(wrap_untrusted(text, source=cap, step=i) for i, cap, text in results)
            user = f"Request:\n{self.task.goal}\n\nCapability results:\n{body}"
        else:
            user = self.task.goal
        messages = [{"role": "system", "content": ANSWER_SYSTEM}, {"role": "user", "content": user}]
        self.tracker.start_model_call()
        budget = self.tracker.request_budget()
        self.task.emit("model_call", kind="answer", n=self.tracker.model_calls)
        parts: list[str] = []
        async with self.deps.admission.slot():
            async for chunk in self.deps.provider.stream(messages, budget, seed=self.deps.seed):
                if chunk.usage is not None:
                    self.tracker.record_usage(chunk.usage)
                elif chunk.text:
                    parts.append(chunk.text)
                    self.task.emit("token", t=chunk.text)
        answer = "".join(parts).strip() or "(The model returned an empty answer.)"
        self.task.emit("answer", text=answer)
        return answer


ARMS: dict[str, type[TaskRunner]] = {"react": ReActRunner, "plan_execute": PlanExecuteRunner}
