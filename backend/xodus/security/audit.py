"""Append-only, hash-chained audit log (master prompt section 8).

Each line is a JSON record carrying a sequence number and the SHA-256 of the previous record, so an
edit, deletion or reordering of earlier records is detectable with verify(). This makes tampering
evident; it cannot prevent it, because anyone with the user's file permissions can rewrite the file.
Removing the newest records leaves a valid shorter chain; the head hash is therefore also written
to the main log at startup as a weak external anchor.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xodus.security.secrets import known as known_secrets

GENESIS = "0" * 64
_SECRET_KEY = re.compile(r"(secret|token|password|passwd|api[_-]?key|authorization|cookie)", re.IGNORECASE)
_RESERVED_FIELDS = {"seq", "ts", "event", "prev", "hash"}
MAX_STRING = 2000


def _canonical(record: dict[str, Any]) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def redact(value: Any, secrets: Iterable[str] = ()) -> Any:
    """Mask values under secret-looking keys and any occurrence of a known secret value."""
    secrets = [s for s in secrets if s]
    if isinstance(value, dict):
        return {k: "[redacted]" if _SECRET_KEY.search(str(k)) else redact(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[redacted]")
        return value if len(value) <= MAX_STRING else value[:MAX_STRING] + f"...[{len(value) - MAX_STRING} chars cut]"
    return value


@dataclass(frozen=True)
class AuditVerification:
    ok: bool
    records: int
    first_bad_seq: int | None = None
    reason: str | None = None


class AuditLog:
    def __init__(self, path: Path, secrets: Iterable[str] = ()) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._secrets = [s for s in secrets if len(s) >= 6]
        self.seq, self.head = self._read_tail()

    def add_secret(self, value: str) -> None:
        if len(value) >= 6:
            self._secrets.append(value)

    def _read_tail(self) -> tuple[int, str]:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return 0, GENESIS
        with open(self.path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 65536))
            lines = [line for line in fh.read().splitlines() if line.strip()]
        last = json.loads(lines[-1])
        return int(last["seq"]), str(last["hash"])

    def append(self, event: str, **fields: Any) -> dict[str, Any]:
        clean = {k: v for k, v in redact(fields, [*self._secrets, *known_secrets()]).items() if k not in _RESERVED_FIELDS}
        with self._lock:
            record: dict[str, Any] = {
                "seq": self.seq + 1,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "event": event,
                **clean,
                "prev": self.head,
            }
            record = json.loads(json.dumps(record, default=str))  # hash exactly what is stored
            record["hash"] = hashlib.sha256(_canonical(record)).hexdigest()
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self.seq, self.head = record["seq"], record["hash"]
            return record


def read_recent(path: Path, limit: int = 50, before_seq: int | None = None, event_prefix: str | None = None,
                max_scan_bytes: int = 32 * 2**20) -> list[dict[str, Any]]:
    """Newest records first, for display. Reads the file backwards in blocks, so a long log is not loaded whole.
    Display only: it does not check the chain (that is verify()). Lines that are not JSON objects are skipped."""
    if not path.exists():
        return []
    limit = max(1, min(int(limit), 500))
    out: list[dict[str, Any]] = []
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        scanned, tail = 0, b""
        while pos > 0 and len(out) < limit and scanned < max_scan_bytes:
            step = min(65536, pos)
            pos -= step
            fh.seek(pos)
            block = fh.read(step) + tail
            scanned += step
            lines = block.split(b"\n")
            tail = lines.pop(0) if pos > 0 else b""  # possibly a cut line: completed by the next (earlier) block
            for raw in reversed(lines):
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                if before_seq is not None and int(record.get("seq", 0)) >= before_seq:
                    continue
                if event_prefix and not str(record.get("event", "")).startswith(event_prefix):
                    continue
                out.append(record)
                if len(out) >= limit:
                    break
    return out


def verify(path: Path) -> AuditVerification:
    """Check sequence numbers, the previous-hash chain and each record's own hash."""
    if not path.exists():
        return AuditVerification(True, 0)
    prev, expected, count = GENESIS, 1, 0
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                return AuditVerification(False, count, expected, f"line {lineno} is not valid JSON")
            if not isinstance(record, dict):
                return AuditVerification(False, count, expected, f"line {lineno} is not a JSON object")
            stored = record.pop("hash", None)
            if record.get("seq") != expected:
                return AuditVerification(False, count, expected, f"line {lineno}: expected seq {expected}, found {record.get('seq')}")
            if record.get("prev") != prev:
                return AuditVerification(False, count, expected, f"line {lineno}: chain broken (previous hash does not match)")
            if hashlib.sha256(_canonical(record)).hexdigest() != stored:
                return AuditVerification(False, count, expected, f"line {lineno}: record content does not match its hash")
            prev, expected, count = stored, expected + 1, count + 1
    return AuditVerification(True, count)
