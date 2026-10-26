"""Approvals bound to the exact action (master prompt section 5 and the approval row of section 8).

An approval request carries the SHA-256 of (task, step, capability id and version, canonical
arguments, resolved targets). Approving requires presenting that hash, so the decision applies to
exactly what was shown. Executing requires consuming the approval with the hash of the action that
is about to run: a different action (modified arguments, another step, another task) does not
match. Approvals are single-use and expire. Every decision and every refusal is audited.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from xodus.security.audit import AuditLog

TERMINAL = {"denied", "expired", "consumed", "withdrawn"}


def action_hash(
    *,
    task_id: str,
    step_index: int,
    capability_id: str,
    capability_version: str,
    args: Any,
    targets: list[str],
) -> str:
    payload = {
        "task": task_id,
        "step": step_index,
        "capability": capability_id,
        "version": capability_version,
        "args": args,
        "targets": sorted(targets),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ApprovalError(Exception):
    STATUS = {"not_found": 404, "expired": 410}

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = self.STATUS.get(code, 409)


@dataclass
class ApprovalRequest:
    id: str
    task_id: str
    step_index: int
    capability_id: str
    capability_name: str
    capability_version: str
    args: Any
    targets: list[str]
    side_effects: str
    undoable: bool | None
    reasons: list[str]
    action_hash: str
    created_at: float
    expires_at: float
    status: str = "pending"
    decided_at: float | None = None
    _event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def public(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


class ApprovalStore:
    def __init__(self, audit: AuditLog, ttl_s: float = 600.0, clock: Callable[[], float] = time.time,
                 max_kept: int = 200) -> None:
        self._audit = audit
        self.ttl_s = ttl_s
        self._clock = clock
        self._max_kept = max_kept
        self._items: dict[str, ApprovalRequest] = {}

    def request(
        self,
        *,
        task_id: str,
        step_index: int,
        capability_id: str,
        capability_name: str,
        capability_version: str,
        args: Any,
        targets: list[str],
        side_effects: str,
        undoable: bool | None,
        reasons: list[str],
        hash_: str,
    ) -> ApprovalRequest:
        now = self._clock()
        req = ApprovalRequest(
            id=secrets.token_urlsafe(16),
            task_id=task_id,
            step_index=step_index,
            capability_id=capability_id,
            capability_name=capability_name,
            capability_version=capability_version,
            args=args,
            targets=targets,
            side_effects=side_effects,
            undoable=undoable,
            reasons=reasons,
            action_hash=hash_,
            created_at=now,
            expires_at=now + self.ttl_s,
        )
        self._items[req.id] = req
        self._prune()
        self._audit.append("approval_requested", approval_id=req.id, task_id=task_id, step=step_index,
                           capability=capability_id, args=args, targets=targets, reasons=reasons,
                           action_hash=hash_, expires_in_s=self.ttl_s)
        return req

    def _prune(self) -> None:
        done = [r for r in self._items.values() if r.status in TERMINAL]
        for old in sorted(done, key=lambda r: r.created_at)[: max(0, len(self._items) - self._max_kept)]:
            self._items.pop(old.id, None)

    def _get(self, approval_id: str) -> ApprovalRequest:
        req = self._items.get(approval_id)
        if req is None:
            raise ApprovalError("not_found", "No approval request with that id.")
        self._refresh(req)
        return req

    def _refresh(self, req: ApprovalRequest) -> None:
        if req.status in ("pending", "approved") and self._clock() >= req.expires_at:
            req.status = "expired"
            self._audit.append("approval_expired", approval_id=req.id, task_id=req.task_id)
            req._event.set()

    def _refuse(self, req: ApprovalRequest, code: str, message: str, **extra: Any) -> ApprovalError:
        self._audit.append("approval_rejected", approval_id=req.id, task_id=req.task_id, reason=code, **extra)
        return ApprovalError(code, message)

    def get(self, approval_id: str) -> ApprovalRequest:
        return self._get(approval_id)

    def list(self, status: str | None = None) -> list[ApprovalRequest]:
        for req in list(self._items.values()):
            self._refresh(req)
        return [r for r in self._items.values() if status in (None, r.status)]

    def resolve(self, approval_id: str, decision: str, presented_hash: str) -> ApprovalRequest:
        """Record the user's decision. The presented hash must equal the hash that was shown."""
        req = self._get(approval_id)
        if req.status == "expired":
            raise self._refuse(req, "expired", "This approval request has expired.")
        if req.status != "pending":
            raise self._refuse(req, "already_resolved", f"This approval request is already {req.status}.")
        if decision not in ("approve", "deny"):
            raise ApprovalError("invalid_decision", "decision must be 'approve' or 'deny'.")
        if not hmac.compare_digest(str(presented_hash), req.action_hash):
            raise self._refuse(req, "hash_mismatch", "The action hash does not match the requested action.",
                               presented=str(presented_hash)[:64])
        req.status = "approved" if decision == "approve" else "denied"
        req.decided_at = self._clock()
        self._audit.append("approval_granted" if decision == "approve" else "approval_denied",
                           approval_id=req.id, task_id=req.task_id, action_hash=req.action_hash)
        req._event.set()
        return req

    async def wait(self, approval_id: str) -> str:
        """Wait until the request is decided or expires; return its status."""
        req = self._get(approval_id)
        remaining = req.expires_at - self._clock()
        if req.status == "pending" and remaining > 0:
            try:
                await asyncio.wait_for(req._event.wait(), timeout=remaining)
            except TimeoutError:
                pass
        self._refresh(req)
        return req.status

    def consume(self, approval_id: str, current_hash: str) -> None:
        """Use an approval exactly once, for exactly the approved action."""
        req = self._get(approval_id)
        if req.status == "consumed":
            raise self._refuse(req, "replayed", "This approval has already been used.")
        if req.status != "approved":
            raise self._refuse(req, "not_approved", f"This approval is {req.status}, not approved.")
        if not hmac.compare_digest(current_hash, req.action_hash):
            raise self._refuse(req, "hash_mismatch", "The action about to run differs from the approved action.",
                               presented=current_hash[:64])
        req.status = "consumed"
        self._audit.append("approval_consumed", approval_id=req.id, task_id=req.task_id, action_hash=req.action_hash)

    def withdraw_for_task(self, task_id: str) -> None:
        for req in self._items.values():
            if req.task_id == task_id and req.status in ("pending", "approved"):
                req.status = "withdrawn"
                self._audit.append("approval_withdrawn", approval_id=req.id, task_id=task_id)
                req._event.set()
