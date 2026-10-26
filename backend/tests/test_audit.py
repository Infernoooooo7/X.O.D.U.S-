"""Hash-chained audit log: appends verify; edits, deletions and reordering are detected; secrets are redacted."""

from __future__ import annotations

import json

from xodus.security.audit import AuditLog, verify


def _fill(path, n=5):
    log = AuditLog(path)
    for i in range(n):
        log.append("capability_call", task_id="t1", step=i, capability="fs.list_files", args={"path": f"p{i}"})
    return log


def test_appends_verify(tmp_path):
    path = tmp_path / "audit.jsonl"
    _fill(path)
    result = verify(path)
    assert result.ok and result.records == 5


def test_chain_continues_across_restarts(tmp_path):
    path = tmp_path / "audit.jsonl"
    _fill(path, 3)
    log = AuditLog(path)  # a new process picks up seq and head hash
    assert log.seq == 3
    log.append("approval_granted", approval_id="x")
    assert verify(path).ok and verify(path).records == 4


def test_edited_record_is_detected(tmp_path):
    path = tmp_path / "audit.jsonl"
    _fill(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[2])
    record["args"] = {"path": "something else"}
    lines[2] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = verify(path)
    assert not result.ok and result.first_bad_seq == 3 and "hash" in result.reason


def test_deleted_record_is_detected(tmp_path):
    path = tmp_path / "audit.jsonl"
    _fill(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    del lines[1]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = verify(path)
    assert not result.ok and result.first_bad_seq == 2


def test_reordered_records_are_detected(tmp_path):
    path = tmp_path / "audit.jsonl"
    _fill(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1], lines[2] = lines[2], lines[1]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert not verify(path).ok


def test_recomputed_hash_still_breaks_the_chain(tmp_path):
    """Rewriting one record *and* its own hash is caught by the next record's prev link."""
    import hashlib

    from xodus.security.audit import _canonical

    path = tmp_path / "audit.jsonl"
    _fill(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[1])
    record.pop("hash")
    record["args"] = {"path": "forged"}
    record["hash"] = hashlib.sha256(_canonical(record)).hexdigest()
    lines[1] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = verify(path)
    assert not result.ok and result.first_bad_seq == 3 and "chain broken" in result.reason


def test_secrets_are_redacted(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, secrets=["canary-SECRET-4417"])
    log.append("plugin_call", api_key="abc", nested={"Authorization": "Bearer x", "ok": "fine"},
               note="the value canary-SECRET-4417 leaked into text")
    text = path.read_text(encoding="utf-8")
    assert "canary-SECRET-4417" not in text and "Bearer x" not in text and '"abc"' not in text
    assert "fine" in text and verify(path).ok


def test_reserved_fields_cannot_be_overridden(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append("x", seq=999, hash="0" * 64, prev="f" * 64)
    assert verify(path).ok and log.seq == 1
