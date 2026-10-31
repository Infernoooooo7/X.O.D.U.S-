"""Process supervisor and Windows Job Objects: children die with the backend, even on a hard kill;
idle reaping; orphan clean-up; PID-reuse guard; optional-process limit; breakaway attempts."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psutil
import pytest

from tests.conftest import FIXTURES, free_port
from xodus.resources.sampler import listener_pid
from xodus.security.token import HEADER as TOKEN_HEADER
from xodus.security.token import read_token
from xodus.supervisor.jobs import JobObject
from xodus.supervisor.supervisor import Supervisor, SupervisorLimit

BACKEND_DIR = Path(__file__).resolve().parents[1]
SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


def gone(pids, timeout=10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not [p for p in pids if psutil.pid_exists(p)]:
            return True
        time.sleep(0.05)
    return False


def launch(sup, command=SLEEPER, tier="optional", **kw):
    return sup.launch(command, role="test", owner="test", start_reason="test", cwd=Path.cwd(), env=None, tier=tier, **kw)


def test_children_die_when_the_backend_is_hard_killed():
    """The host process owns the job handle. Killing it with TerminateProcess runs no cleanup code;
    Windows closes the handle and KILL_ON_JOB_CLOSE takes the child and the grandchild with it."""
    host = subprocess.Popen([sys.executable, str(FIXTURES / "supervisor_host.py")], stdout=subprocess.PIPE, text=True)
    try:
        info = json.loads(host.stdout.readline())
        pids = set(info["job"]) | {info["child"], info["grandchild"]}
        assert info["grandchild"] in info["job"], "the grandchild must be inside the job"
        assert all(psutil.pid_exists(p) for p in pids)
        psutil.Process(info["host"]).kill()  # the interpreter holding the job handle
        host.wait(timeout=15)
        assert gone(pids), [p for p in pids if psutil.pid_exists(p)]
    finally:
        if host.poll() is None:
            host.kill()


def test_real_backend_hard_kill_takes_its_plugin_processes_with_it(tmp_path):
    """Same property end to end: a real `python -m xodus` runs a plugin that spawned a grandchild."""
    port = free_port()
    env = {**os.environ,
           "XODUS_SERVER__PORT": str(port),
           "XODUS_DATA_DIR": str(tmp_path / "data"),
           "XODUS_LOG_DIR": str(tmp_path / "logs"),
           "XODUS_PLUGINS__DIR": str(FIXTURES / "plugins_e2e"),
           "XODUS_PERMISSIONS__GLOBAL_GRANTS": '["exec.spawn"]'}
    backend = subprocess.Popen([sys.executable, "-m", "xodus"], cwd=BACKEND_DIR, env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{base}/api/health", trust_env=False, timeout=2).status_code in (200, 401):  # up; 401 = no token yet
                    break
            except httpx.HTTPError:
                time.sleep(0.3)
        auth = {TOKEN_HEADER: read_token(tmp_path / "data")}
        caps = {c["id"]: c for c in httpx.get(f"{base}/api/capabilities", headers=auth, trust_env=False).json()}
        assert caps["test.spawner"]["available"], caps["test.spawner"]["unavailable_reasons"]
        result = httpx.post(f"{base}/api/capabilities/test.spawner/invoke", json={"args": {}}, headers=auth, trust_env=False,
                            timeout=20).json()
        assert result["ok"], result
        supervised = httpx.get(f"{base}/api/processes", headers=auth, trust_env=False, timeout=10).json()["supervised"]
        assert len(supervised) == 1 and supervised[0]["tier"] == "optional" and supervised[0]["owner"] == "test.spawner"
        pids = set(supervised[0]["job_pids"]) | {result["result"]["grandchild"], result["result"]["pid"]}
        assert result["result"]["grandchild"] in supervised[0]["job_pids"]
        assert all(psutil.pid_exists(p) for p in pids)
        psutil.Process(listener_pid(port)).kill()  # hard kill of the interpreter serving the API
        backend.wait(timeout=15)
        assert gone(pids), [p for p in pids if psutil.pid_exists(p)]
    finally:
        if backend.poll() is None:
            backend.kill()


def test_stop_all_terminates_whole_trees():
    sup = Supervisor(max_optional=3)
    code = ("import subprocess, sys, time; g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
            " print(g.pid, flush=True); time.sleep(60)")
    procs = [launch(sup, [sys.executable, "-c", code]) for _ in range(2)]
    pids = set()
    for mp in procs:
        pids.add(int(mp.popen.stdout.readline()))
        pids.update(mp.job.pids())
    sup.stop_all("test")
    assert gone(pids) and sup.managed() == []


def test_idle_optional_process_is_reaped():
    sup = Supervisor(idle_timeout_s=0.3)
    mp = launch(sup)
    pids = mp.job.pids()
    assert sup.reap() == []  # not idle yet
    time.sleep(0.6)
    assert sup.reap() == [mp]
    assert gone(pids) and sup.managed() == []


def test_reaper_cleans_up_grandchildren_whose_parent_exited():
    sup = Supervisor()
    code = "import subprocess, sys; g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); print(g.pid, flush=True)"
    mp = launch(sup, [sys.executable, "-c", code])
    grandchild = int(mp.popen.stdout.readline())
    mp.popen.wait(timeout=15)  # the root exits; the grandchild would otherwise live on as an orphan
    assert psutil.pid_exists(grandchild) and grandchild in mp.job.pids()
    assert sup.reap() == [mp]
    assert gone([grandchild])


def test_pid_reuse_guard_and_job_only_termination():
    sup = Supervisor()
    mp = launch(sup)
    stale = dataclasses.replace(mp, create_time=mp.create_time - 1000)  # same PID, different process
    assert sup.is_alive(mp) and not sup.is_alive(stale)
    unrelated = subprocess.Popen(SLEEPER)
    empty = JobObject()
    empty.terminate()  # the only way the supervisor stops anything is through its own job
    empty.close()
    assert unrelated.poll() is None
    unrelated.kill()
    sup.stop_all()


def test_optional_process_limit_core_unaffected():
    sup = Supervisor(max_optional=1)
    try:
        launch(sup)
        with pytest.raises(SupervisorLimit):
            launch(sup)
        launch(sup, tier="core")  # core processes (e.g. the ingestion worker) are not capped here
        assert len(sup.managed()) == 2
    finally:
        sup.stop_all()


def test_breakaway_attempt_does_not_escape_the_job():
    """[VERIFY] A child asks Windows to start a process outside its job (CREATE_BREAKAWAY_FROM_JOB).
    Either Windows refuses, or the new process must still be in our job and die with it."""
    code = (
        "import subprocess, sys\n"
        "try:\n"
        "    p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], creationflags=0x01000000)\n"
        "    print(p.pid, flush=True)\n"
        "except OSError as exc:\n"
        "    print('refused', exc.winerror, flush=True)\n"
        "import time; time.sleep(60)\n"
    )
    sup = Supervisor()
    mp = launch(sup, [sys.executable, "-c", code])
    line = mp.popen.stdout.readline().decode().strip()
    job_pids = mp.job.pids()
    sup.stop_all()
    if line.startswith("refused"):
        assert line.split()[1] == "5"  # ERROR_ACCESS_DENIED
    else:
        escaped = int(line)
        try:
            assert escaped in job_pids, f"process {escaped} escaped the job"
            assert gone([escaped])
        finally:
            if psutil.pid_exists(escaped):
                psutil.Process(escaped).kill()
