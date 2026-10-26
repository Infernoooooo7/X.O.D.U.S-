"""Minimal capability registry (Gate B).

A capability declares a manifest and a Pydantic input model; the registry validates input
against that model, runs the capability with a timeout, and enforces the output byte cap.
Discovery, permission policy and approvals arrive in Gate C.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError


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


@dataclass(frozen=True)
class CapabilityManifest:
    id: str
    name: str
    description: str
    version: str
    permissions: tuple[str, ...]
    execution_mode: str = "in_process"
    process_tier: str = "none"
    resource_class: str = "light"
    side_effects: str = "none"
    requires_approval: str = "never"
    timeout_s: float = 5.0
    max_output_bytes: int = 256_000
    cancellable: bool = False


class Capability(Protocol):
    manifest: CapabilityManifest
    input_model: type[BaseModel]

    def run(self, args: BaseModel) -> dict[str, Any]: ...


class CapabilityRegistry:
    def __init__(self) -> None:
        self._caps: dict[str, Capability] = {}

    def register(self, cap: Capability) -> None:
        if cap.manifest.id in self._caps:
            raise ValueError(f"capability {cap.manifest.id!r} registered twice")
        self._caps[cap.manifest.id] = cap

    def describe(self) -> list[dict[str, Any]]:
        return [
            {**asdict(cap.manifest), "input_schema": cap.input_model.model_json_schema(), "availability": "available"}
            for cap in self._caps.values()
        ]

    async def invoke(self, cap_id: str, raw_args: dict[str, Any]) -> dict[str, Any]:
        cap = self._caps.get(cap_id)
        if cap is None:
            raise UnknownCapability(f"No capability named {cap_id!r} is registered.")
        try:
            args = cap.input_model.model_validate(raw_args)
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'input'}: {e['msg']}" for e in exc.errors())
            raise InvalidInput(f"Invalid input for {cap_id}: {problems}") from exc
        try:
            result = await asyncio.wait_for(asyncio.to_thread(cap.run, args), timeout=cap.manifest.timeout_s)
        except TimeoutError as exc:
            raise CapabilityTimeout(f"{cap_id} did not finish within {cap.manifest.timeout_s:.0f} s.") from exc
        size = len(json.dumps(result).encode("utf-8"))
        if size > cap.manifest.max_output_bytes:
            raise OutputTooLarge(f"{cap_id} produced {size} bytes, over its {cap.manifest.max_output_bytes}-byte cap.")
        return result
