"""StepGate: the single enforcement point every capability call passes through (master prompt 5, 8).

Order of checks for a proposed step (all in code; prompt wording is never relied upon):
  1. the capability exists                         -> else deny unknown_capability
  2. it was offered for this task (fixed at start) -> else deny not_offered
  3. it is available (config, grants, health)      -> else deny unavailable
  4. every permission it declares is granted       -> else deny permission_denied
  5. the arguments validate against its schema     -> else deny invalid_arguments
     (schemas forbid undeclared arguments, so a model cannot smuggle in "root" or "permissions")
  6. path arguments resolve inside their root      -> else deny path_violation / not_found
  7. approval is required when the capability says so, when it has side effects outside the XODUS
     data folder or external effects, or when the task has already read untrusted content and the
     step has any side effect                      -> approve (with the action hash)
  8. otherwise                                     -> allow
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator

from xodus.capabilities.registry import Capability, CapabilityRegistry
from xodus.security.approvals import action_hash
from xodus.security.paths import PathViolation, resolve_path
from xodus.security.policy import PermissionPolicy


@dataclass(frozen=True)
class ProposedStep:
    index: int
    capability_id: Any  # untrusted: straight from a model or a request
    args: Any  # untrusted
    origin: Literal["react", "plan", "replan", "user"]


@dataclass
class GateContext:
    task_id: str
    allowed: frozenset[str]
    tainted: bool = False


@dataclass
class GateDecision:
    verdict: Literal["allow", "approve", "deny"]
    code: str
    message: str
    capability: Capability | None = None
    args: dict[str, Any] | None = None
    targets: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    action_hash: str | None = None


def _deny(code: str, message: str, cap: Capability | None = None) -> GateDecision:
    return GateDecision("deny", code, message, capability=cap)


class StepGate:
    def __init__(self, registry: CapabilityRegistry, policy: PermissionPolicy, data_dir: Path) -> None:
        self.registry = registry
        self.policy = policy
        self.data_dir = data_dir.resolve() if data_dir.exists() else data_dir

    def evaluate(self, step: ProposedStep, ctx: GateContext) -> GateDecision:
        cap_id = step.capability_id
        cap = self.registry.get(cap_id) if isinstance(cap_id, str) else None
        if cap is None:
            return _deny("unknown_capability", f"There is no capability named {str(cap_id)[:80]!r}.")
        if cap_id not in ctx.allowed:
            return _deny("not_offered", f"{cap_id} was not offered for this task, so it cannot be used.", cap)
        unavailable = self.registry.unavailable_reasons(cap_id)
        if unavailable:
            return _deny("unavailable", f"{cap_id} is unavailable: {'; '.join(unavailable)}.", cap)
        manifest = cap.manifest
        missing = self.policy.missing(manifest.permissions)
        if missing:  # unreachable while availability includes grants; kept as an explicit check
            return _deny("permission_denied", f"{cap_id} needs permissions that are not granted: {'; '.join(missing)}.", cap)

        if not isinstance(step.args, dict):
            return _deny("invalid_arguments", f"Arguments for {cap_id} must be a JSON object.", cap)
        args = copy.deepcopy(step.args)
        try:
            errors = sorted(Draft202012Validator(manifest.input_schema).iter_errors(args), key=lambda e: list(e.path))
        except Exception as exc:  # e.g. an unresolvable local $ref in a plugin's schema
            return _deny("invalid_arguments", f"Arguments for {cap_id} could not be checked: {type(exc).__name__}.", cap)
        if errors:
            details = "; ".join(f"{'/'.join(map(str, e.path)) or 'arguments'}: {e.message[:150]}" for e in errors[:3])
            return _deny("invalid_arguments", f"Invalid arguments for {cap_id}: {details}", cap)

        targets: list[str] = []
        props = manifest.input_schema.get("properties", {})
        for name, (permission, kind) in manifest.path_arguments().items():
            value = args.get(name, props.get(name, {}).get("default"))
            if value is None:
                continue
            root = self.policy.root_for(permission)
            try:
                targets.append(str(resolve_path(root.path, value, kind)))
            except PathViolation as exc:
                return _deny(exc.code if exc.code != "invalid_input" else "invalid_arguments",
                             f"{cap_id}.{name}: {exc.message}", cap)

        reasons: list[str] = []
        if manifest.requires_approval == "always":
            reasons.append("this capability always asks for approval")
        if manifest.side_effects == "external":
            reasons.append("it has effects outside this computer")
        if manifest.side_effects == "local":
            if manifest.requires_approval == "consequential":
                reasons.append("it changes files")
            outside = [t for t in targets if not Path(t).is_relative_to(self.data_dir)]
            if outside or not targets:
                reasons.append("it writes outside the XODUS data folder")
        if ctx.tainted and manifest.side_effects != "none":
            reasons.append("the task has already read untrusted content")
        if reasons:
            digest = action_hash(task_id=ctx.task_id, step_index=step.index, capability_id=cap_id,
                                 capability_version=manifest.version, args=args, targets=targets)
            return GateDecision("approve", "approval_required", "Approval required: " + "; ".join(dict.fromkeys(reasons)),
                                capability=cap, args=args, targets=targets, reasons=list(dict.fromkeys(reasons)),
                                action_hash=digest)
        return GateDecision("allow", "ok", "allowed", capability=cap, args=args, targets=targets)
