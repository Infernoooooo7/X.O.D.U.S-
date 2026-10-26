"""Approvals: bound to the action hash, single-use, expiring, audited (security table: approval bypass)."""

from __future__ import annotations

import asyncio

import pytest

from xodus.security.approvals import ApprovalError, ApprovalStore, action_hash
from xodus.security.audit import AuditLog, verify


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


ARGS = {"path": "note.txt", "text": "hello"}


def _hash(**over):
    base = dict(task_id="t1", step_index=1, capability_id="test.write_note", capability_version="1.0.0",
                args=ARGS, targets=["C:\\sandbox\\note.txt"])
    base.update(over)
    return action_hash(**base)


def _request(store, h=None):
    return store.request(task_id="t1", step_index=1, capability_id="test.write_note", capability_name="Write note",
                         capability_version="1.0.0", args=ARGS, targets=["C:\\sandbox\\note.txt"], side_effects="local",
                         undoable=False, reasons=["it changes files"], hash_=h or _hash())


@pytest.fixture
def store(tmp_path):
    clock = Clock()
    s = ApprovalStore(AuditLog(tmp_path / "audit.jsonl"), ttl_s=60, clock=clock)
    s.clock = clock
    return s


def _events(store):
    import json

    return [json.loads(line)["event"] for line in store._audit.path.read_text().splitlines()]


def test_hash_changes_with_every_bound_field():
    base = _hash()
    assert base == _hash()  # deterministic
    assert base == _hash(args={"text": "hello", "path": "note.txt"})  # key order does not matter
    for change in (dict(task_id="t2"), dict(step_index=2), dict(capability_id="test.other"),
                   dict(capability_version="1.0.1"), dict(args={**ARGS, "text": "hello!"}),
                   dict(targets=["C:\\sandbox\\other.txt"])):
        assert _hash(**change) != base, change


def test_approve_then_consume_once(store):
    req = _request(store)
    store.resolve(req.id, "approve", req.action_hash)
    store.consume(req.id, req.action_hash)
    assert store.get(req.id).status == "consumed"
    with pytest.raises(ApprovalError) as replay:
        store.consume(req.id, req.action_hash)
    assert replay.value.code == "replayed"
    assert "approval_rejected" in _events(store)


def test_resolving_twice_is_refused(store):
    req = _request(store)
    store.resolve(req.id, "approve", req.action_hash)
    with pytest.raises(ApprovalError) as again:
        store.resolve(req.id, "approve", req.action_hash)
    assert again.value.code == "already_resolved"


def test_wrong_hash_on_approve_is_refused_and_request_stays_pending(store):
    req = _request(store)
    with pytest.raises(ApprovalError) as bad:
        store.resolve(req.id, "approve", _hash(args={**ARGS, "path": "..\\evil.txt"}))
    assert bad.value.code == "hash_mismatch"
    assert store.get(req.id).status == "pending"


def test_modified_action_cannot_consume_an_approval(store):
    req = _request(store)
    store.resolve(req.id, "approve", req.action_hash)
    with pytest.raises(ApprovalError) as modified:
        store.consume(req.id, _hash(args={**ARGS, "text": "rm -rf"}))
    assert modified.value.code == "hash_mismatch"
    assert store.get(req.id).status == "approved"  # still unused; the original action could run


def test_approval_for_another_task_or_step_does_not_match(store):
    req = _request(store)
    store.resolve(req.id, "approve", req.action_hash)
    for other in (_hash(task_id="t2"), _hash(step_index=2)):
        with pytest.raises(ApprovalError):
            store.consume(req.id, other)


def test_denied_cannot_be_consumed(store):
    req = _request(store)
    store.resolve(req.id, "deny", req.action_hash)
    with pytest.raises(ApprovalError) as denied:
        store.consume(req.id, req.action_hash)
    assert denied.value.code == "not_approved"


def test_expired_request_cannot_be_approved_or_consumed(store):
    req = _request(store)
    store.clock.now += 61
    with pytest.raises(ApprovalError) as late:
        store.resolve(req.id, "approve", req.action_hash)
    assert late.value.code == "expired" and late.value.status == 410
    assert store.get(req.id).status == "expired"


def test_approval_expires_between_approve_and_use(store):
    req = _request(store)
    store.resolve(req.id, "approve", req.action_hash)
    store.clock.now += 61
    with pytest.raises(ApprovalError) as late:
        store.consume(req.id, req.action_hash)
    assert late.value.code == "not_approved"
    assert store.get(req.id).status == "expired"


def test_unknown_id_and_bad_decision(store):
    with pytest.raises(ApprovalError) as missing:
        store.resolve("nope", "approve", "x")
    assert missing.value.status == 404
    req = _request(store)
    with pytest.raises(ApprovalError) as bad:
        store.resolve(req.id, "maybe", req.action_hash)
    assert bad.value.code == "invalid_decision"


async def test_wait_times_out_to_expired(tmp_path):
    store = ApprovalStore(AuditLog(tmp_path / "audit.jsonl"), ttl_s=0.2)
    req = _request(store)
    assert await store.wait(req.id) == "expired"


async def test_wait_returns_on_decision(tmp_path):
    store = ApprovalStore(AuditLog(tmp_path / "audit.jsonl"), ttl_s=10)
    req = _request(store)
    waiter = asyncio.create_task(store.wait(req.id))
    await asyncio.sleep(0.05)
    store.resolve(req.id, "approve", req.action_hash)
    assert await asyncio.wait_for(waiter, 2) == "approved"


def test_withdraw_on_cancel(store):
    req = _request(store)
    store.withdraw_for_task("t1")
    with pytest.raises(ApprovalError):
        store.resolve(req.id, "approve", req.action_hash)


def test_every_decision_is_audited(store):
    a = _request(store)
    store.resolve(a.id, "approve", a.action_hash)
    store.consume(a.id, a.action_hash)
    b = _request(store)
    store.resolve(b.id, "deny", b.action_hash)
    events = _events(store)
    for expected in ("approval_requested", "approval_granted", "approval_consumed", "approval_denied"):
        assert expected in events
    assert verify(store._audit.path).ok
