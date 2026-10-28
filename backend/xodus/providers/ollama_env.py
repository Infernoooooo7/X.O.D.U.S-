"""Was the running Ollama server started the way XODUS needs it (master prompt rule 13, 3.8 and section 8)?

XODUS does not start Ollama: scripts/start-ollama.ps1 does, and a user may start it some other way (the Ollama desktop
app, a service, a shell with other variables). So the backend looks at the server it actually talks to: the process
listening on the Ollama port, its listening addresses (psutil.net_connections) and its environment (psutil environ).

Four checks: listening on loopback only; OLLAMA_NO_CLOUD switched on; OLLAMA_REMOTES not set; no wildcard in
OLLAMA_ORIGINS. 'ok' means exactly that these four passed. It does not prove Ollama never contacts the internet:
settings Ollama reads from anywhere other than its environment are not visible here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psutil

from xodus.config.settings import is_loopback_host

# Ollama reads booleans with Go's strconv.ParseBool after trimming spaces and quotes; these are its true spellings.
_TRUE = {"1", "t", "true"}
FIX = "Restart Ollama with scripts\\stop-ollama.ps1 and then scripts\\start-ollama.ps1, which set these for Ollama only."


def _value(env: dict[str, str], key: str) -> str:
    return env.get(key, "").strip().strip("\"'")


def _check(id_: str, ok: bool | None, detail: str) -> dict[str, Any]:
    return {"id": id_, "ok": ok, "detail": detail}


def _environment_checks(env: dict[str, str]) -> list[dict[str, Any]]:
    no_cloud, remotes, origins = (_value(env, k) for k in ("OLLAMA_NO_CLOUD", "OLLAMA_REMOTES", "OLLAMA_ORIGINS"))
    if no_cloud.lower() in _TRUE:
        cloud = _check("no_cloud", True, f"OLLAMA_NO_CLOUD={no_cloud}: Ollama's cloud models and features are off.")
    elif no_cloud:
        cloud = _check("no_cloud", False, f"OLLAMA_NO_CLOUD is {no_cloud!r}; XODUS expects 1 (cloud features off).")
    else:
        cloud = _check("no_cloud", False, "OLLAMA_NO_CLOUD is not set, so Ollama's cloud models and features are not "
                                          "switched off.")
    return [
        cloud,
        _check("remotes", False, f"OLLAMA_REMOTES is set ({remotes}): Ollama may forward requests for remote models to "
                                 "those hosts.") if remotes else _check("remotes", True, "OLLAMA_REMOTES is not set."),
        _check("origins", False, f"OLLAMA_ORIGINS contains a wildcard ({origins}): web pages from any matching origin "
                                 "can call Ollama through your browser.") if "*" in origins
        else _check("origins", True, f"OLLAMA_ORIGINS={origins} has no wildcard." if origins else "OLLAMA_ORIGINS is not set."),
    ]


def check_ollama_environment(
    port: int,
    *,
    connections: Callable[[], list[Any]] = lambda: psutil.net_connections(kind="tcp"),
    environ: Callable[[int], dict[str, str]] = lambda pid: psutil.Process(pid).environ(),
) -> dict[str, Any]:
    try:
        listeners = [c for c in connections() if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port]
    except psutil.Error as exc:
        return {"status": "unknown", "pid": None, "checks": [], "fix": None,
                "message": f"The listening sockets could not be read ({type(exc).__name__})."}
    if not listeners:
        return {"status": "unknown", "pid": None, "checks": [], "fix": None,
                "message": f"Nothing is listening on port {port}, so the Ollama server's settings could not be read."}
    pid = listeners[0].pid
    exposed = sorted({c.laddr.ip for c in listeners if not is_loopback_host(c.laddr.ip)})
    checks = [_check("loopback", False, f"Ollama listens on {', '.join(exposed)}, not only on loopback: other devices "
                                        "on the network can use it.") if exposed
              else _check("loopback", True, f"Listening on {', '.join(sorted({c.laddr.ip for c in listeners}))} only.")]
    try:
        if pid is None:
            raise psutil.AccessDenied()
        checks += _environment_checks(environ(pid))
        unread = None
    except psutil.Error as exc:
        unread = f"The Ollama process's settings could not be read ({type(exc).__name__})."
        checks += [_check(k, None, unread) for k in ("no_cloud", "remotes", "origins")]
    if any(c["ok"] is False for c in checks):
        status, message = "problem", "Ollama is running with settings that let data or requests leave this computer."
    elif unread:
        status, message = "unknown", unread
    else:
        status, message = "ok", "Ollama runs on loopback with its cloud features off."
    return {"status": status, "pid": pid, "checks": checks, "message": message, "fix": FIX if status == "problem" else None}
