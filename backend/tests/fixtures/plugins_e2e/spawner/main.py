"""Test plugin: each call starts a long-sleeping grandchild process and returns its PID."""

import json
import os
import subprocess
import sys

for line in sys.stdin:
    request = json.loads(line)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    reply = {"jsonrpc": "2.0", "id": request["id"], "result": {"grandchild": child.pid, "pid": os.getpid()}}
    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()
