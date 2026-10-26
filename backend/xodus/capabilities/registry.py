"""Capability registry: registration, visible rejections and availability (master prompt 4.2).

The registry only knows what exists and whether it can run now. Whether a given call may run is
decided by the StepGate (orchestrator/gate.py), which every invocation path goes through.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from xodus.capabilities.manifest import CapabilityManifest
from xodus.obs import log_event
from xodus.security.policy import PermissionPolicy


class CapabilityError(Exception):
    code = "capability_error"
    status = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnknownCapability(CapabilityError):
    code = "unknown_capability"
    status = 404


class InvalidInput(CapabilityError):
    code = "invalid_input"
    status = 422


class PermissionDenied(CapabilityError):
    code = "permission_denied"
    status = 403


class NotFound(CapabilityError):
    code = "not_found"
    status = 404


class CapabilityTimeout(CapabilityError):
    code = "timeout"
    status = 504


class OutputTooLarge(CapabilityError):
    code = "output_too_large"
    status = 500


class InvalidOutput(CapabilityError):
    code = "invalid_output"
    status = 500


class CapabilityCancelled(CapabilityError):
    code = "cancelled"
    status = 499


class PluginCrashed(CapabilityError):
    code = "plugin_crashed"
    status = 502


class PluginError(CapabilityError):
    code = "plugin_error"
    status = 502


@dataclass
class InvocationContext:
    task_id: str | None
    step_index: int
    roots: dict[str, Path] = field(default_factory=dict)  # granted roots this capability declared
    cancel: threading.Event = field(default_factory=threading.Event)


class Capability(Protocol):
    manifest: CapabilityManifest

    async def invoke(self, args: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]: ...

    def health(self) -> list[str]: ...


class InProcessCapability:
    """Base for first-party built-ins: run() executes in a worker thread.

    Python threads cannot be killed. On cancellation ctx.cancel is set and the result is discarded;
    run() should check ctx.cancel between units of work and must leave no partial side effects.
    """

    manifest: CapabilityManifest

    def run(self, args: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
        raise NotImplementedError

    def health(self) -> list[str]:
        return []

    async def invoke(self, args: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(self.run, args, ctx)
        except asyncio.CancelledError:
            ctx.cancel.set()
            raise


@dataclass(frozen=True)
class Rejection:
    source: str
    ref: str
    reasons: tuple[str, ...]


class CapabilityRegistry:
    def __init__(
        self,
        policy: PermissionPolicy,
        disabled: Iterable[str] = (),
        model_capabilities: Callable[[], set[str] | None] = lambda: None,
    ) -> None:
        self.policy = policy
        self.disabled = frozenset(disabled)
        self._model_capabilities = model_capabilities
        self._caps: dict[str, Capability] = {}
        self._sources: dict[str, str] = {}
        self.rejected: list[Rejection] = []

    def register(self, cap: Capability, source: str) -> bool:
        cap_id = cap.manifest.id
        if cap_id in self._caps:
            self.reject(source, cap_id, [f"duplicate id; already provided by {self._sources[cap_id]}"])
            return False
        self._caps[cap_id] = cap
        self._sources[cap_id] = source
        log_event("capability_registered", capability=cap_id, source=source)
        return True

    def reject(self, source: str, ref: str, reasons: Iterable[str]) -> None:
        rejection = Rejection(source, ref, tuple(reasons))
        self.rejected.append(rejection)
        log_event("capability_rejected", source=source, ref=ref, reasons=list(rejection.reasons))

    def get(self, cap_id: str) -> Capability | None:
        return self._caps.get(cap_id) if isinstance(cap_id, str) else None

    def ids(self) -> list[str]:
        return sorted(self._caps)

    def unavailable_reasons(self, cap_id: str) -> list[str]:
        cap = self._caps[cap_id]
        reasons: list[str] = []
        if cap_id in self.disabled:
            reasons.append("disabled in config")
        reasons += [f"permission not granted: {m}" for m in self.policy.missing(cap.manifest.permissions)]
        confirmed = self._model_capabilities()
        for dep in cap.manifest.dependencies:
            kind, _, name = dep.partition(":")
            if kind == "capability" and (name == cap_id or name not in self._caps):
                reasons.append(f"depends on missing capability {name!r}")
            elif kind == "model" and (confirmed is None or name not in confirmed):
                reasons.append(f"needs the model capability {name!r}, which is not confirmed")
        reasons += cap.health()
        return reasons

    def available_ids(self) -> list[str]:
        return [cap_id for cap_id in self.ids() if not self.unavailable_reasons(cap_id)]

    def describe(self) -> list[dict[str, Any]]:
        out = []
        for cap_id in self.ids():
            reasons = self.unavailable_reasons(cap_id)
            out.append({
                **self._caps[cap_id].manifest.model_dump(mode="json"),
                "source": self._sources[cap_id],
                "available": not reasons,
                "unavailable_reasons": reasons,
            })
        return out

    def describe_rejected(self) -> list[dict[str, Any]]:
        return [{"source": r.source, "ref": r.ref, "reasons": list(r.reasons)} for r in self.rejected]
