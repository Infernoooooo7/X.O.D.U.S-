from __future__ import annotations

import os

import pytest

from xodus.resources.sampler import GpuCounters, Nvml, ResourceSampler, read_process, system_memory, vanished
import psutil


def test_own_process_uss_is_readable():
    sample = read_process(psutil.Process(), "xodus_app", "core")
    assert sample.uss and sample.rss and sample.private_commit
    assert sample.uss <= sample.rss
    assert not sample.errors


def test_a_process_that_exits_while_sampled_is_dropped_not_unreadable():
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    handle = psutil.Process(proc.pid)
    proc.wait()
    sample = read_process(handle, "ollama", "core")
    assert vanished(sample) and sample.uss is None
    assert not vanished(read_process(psutil.Process(), "xodus_app", "core"))


def test_system_memory():
    mem = system_memory()
    assert 0 < mem["in_use"] < mem["total"]


def test_snapshot_includes_backend_and_never_raises():
    sampler = ResourceSampler(ollama_port=1)  # nothing listens on port 1
    try:
        snap = sampler.snapshot()
    finally:
        sampler.close()
    pids = [p["pid"] for p in snap["processes"]]
    assert os.getpid() in pids
    assert all(p["group"] == "xodus_app" for p in snap["processes"])
    assert snap["totals"]["uss"] > 0


@pytest.mark.gpu
def test_nvml_device_memory():
    nvml = Nvml()
    if nvml.handle is None:
        pytest.skip(f"NVML unavailable: {nvml.error}")
    try:
        mem = nvml.memory()
    finally:
        nvml.close()
    assert mem and mem["total"] > mem["used"] > 0


@pytest.mark.gpu
def test_gpu_process_counters_readable():
    per_pid, discrete = GpuCounters().read()
    if not per_pid:
        pytest.skip("Windows GPU Process Memory counters returned nothing")
    assert discrete and discrete.startswith("luid_")
    assert any(ded > 0 for ded, _ in per_pid.values())  # e.g. dwm.exe on the discrete adapter
