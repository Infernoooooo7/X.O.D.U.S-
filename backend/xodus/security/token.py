"""Per-install API token (master prompt section 8, cross-origin and local-network attacks).

Generated once into data_dir/api_token and required on every /api request, either in the X-XODUS-Token header
(scripts) or in the xodus_token_<port> cookie, which the backend sets on the static UI's pages (HttpOnly, SameSite=Strict,
Path=/), so the browser sends it with the UI's same-origin requests and page scripts never see it.

What it protects against: a web page in the user's browser that reaches the loopback port without a usable token,
including through mistakes in the other defences (Origin and Sec-Fetch-Site checks, Host allow-list). What it cannot
protect against: a process running as the same Windows user, which can read the token file or fetch a UI page; such a
process is outside the threat model (it can already read the data and edit the configuration). Delete the file and
restart the backend to rotate the token.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

HEADER = "X-XODUS-Token"
FILENAME = "api_token"


def cookie_name(port: int) -> str:
    # Cookies are scoped to the host, not the port; the port in the name keeps two backends on 127.0.0.1 apart.
    return f"xodus_token_{port}"


def load_or_create(data_dir: Path) -> str:
    path = data_dir / FILENAME
    try:
        token = path.read_text(encoding="ascii").strip()
        if len(token) >= 32:
            return token
    except (FileNotFoundError, UnicodeDecodeError):
        pass
    token = secrets.token_urlsafe(32)
    tmp = path.with_name(FILENAME + ".tmp")
    tmp.write_text(token, encoding="ascii")
    os.replace(tmp, path)  # atomic: a half-written token is never read
    return token


def matches(supplied: str | None, token: str) -> bool:
    return supplied is not None and secrets.compare_digest(supplied.encode(), token.encode())


def read_token(data_dir: Path) -> str:
    """For local scripts: the token the backend created (start the backend once first)."""
    return (data_dir / FILENAME).read_text(encoding="ascii").strip()
