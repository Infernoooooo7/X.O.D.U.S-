"""Known secret values, and scrubbing them out of everything XODUS writes down or shows a model.

A value is registered when an approved plugin secret is resolved at discovery (XODUS_SECRET_<NAME>). From then on it is
replaced by "[redacted]" in tool output fed to the model, in logs/xodus.jsonl, in logs/tasks.jsonl and in the audit log.
Found by the Gate F canary scan: before this, a plugin that echoed its secret put it into the model's prompt and the
task log. The user still sees a plugin's raw output when invoking it directly.
"""

from __future__ import annotations

import threading
from typing import Any

MIN_LENGTH = 6  # shorter values would redact ordinary words
_lock = threading.Lock()
_values: tuple[str, ...] = ()


def register(value: str) -> None:
    global _values
    if len(value) >= MIN_LENGTH:
        with _lock:
            if value not in _values:
                _values = tuple(sorted({*_values, value}, key=len, reverse=True))  # longest first


def known() -> tuple[str, ...]:
    return _values


def scrub(value: Any) -> Any:
    values = _values
    if not values:
        return value
    if isinstance(value, str):
        for secret in values:
            if secret in value:
                value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    return value
