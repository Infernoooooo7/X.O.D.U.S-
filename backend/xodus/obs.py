"""Structured JSON logging: one JSON object per line, to stdout and logs/xodus.jsonl."""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("xodus")


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)) + f".{int(record.msecs):03d}",
            "level": record.levelname.lower(),
            "event": record.getMessage(),
            **getattr(record, "fields", {}),
        }
        return json.dumps(payload, default=str)


def configure_logging(log_dir: Path, filename: str = "xodus.jsonl", to_stdout: bool = True) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = _JsonFormatter()
    logger.handlers.clear()
    handlers: list[logging.Handler] = [logging.FileHandler(log_dir / filename, encoding="utf-8")]
    if to_stdout:
        handlers.insert(0, logging.StreamHandler(sys.stdout))
    for handler in handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def log_event(event: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, event, extra={"fields": fields})
