"""Test helper that plays the backend: launches a child (which launches a grandchild) under the
Supervisor, prints the PIDs as one JSON line, then sleeps until the test kills it."""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

from xodus.supervisor.supervisor import Supervisor

CHILD = (
    "import subprocess, sys, time; "
    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)']); "
    "print(g.pid, flush=True); time.sleep(600)"
)

supervisor = Supervisor(max_optional=2)
mp = supervisor.launch([sys.executable, "-c", CHILD], role="test-child", owner="test", start_reason="test",
                       cwd=Path(tempfile.gettempdir()), env=None)
grandchild = int(mp.popen.stdout.readline())
time.sleep(0.5)
print(json.dumps({"host": os.getpid(), "child": mp.pid, "grandchild": grandchild, "job": mp.job.pids()}), flush=True)
time.sleep(600)
