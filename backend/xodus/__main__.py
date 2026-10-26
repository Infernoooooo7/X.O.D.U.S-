"""Run the XODUS backend: `python -m xodus` (binds to the configured loopback address only)."""

from __future__ import annotations

import sys

import uvicorn

from xodus.api.app import create_app
from xodus.config import ConfigError, load_settings
from xodus.obs import configure_logging
from xodus.storage.migrate import StorageError


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"XODUS configuration error:\n{exc}", file=sys.stderr)
        return 2
    configure_logging(settings.log_dir)
    try:
        app = create_app(settings)
    except StorageError as exc:  # for example a database built with another embedding size
        print(f"XODUS storage error ({exc.code}):\n{exc.message}", file=sys.stderr)
        return 2
    uvicorn.run(app, host=settings.server.host, port=settings.server.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
