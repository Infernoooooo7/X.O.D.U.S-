from __future__ import annotations

import socket
from pathlib import Path

import httpx
import pytest

from xodus.config.settings import CONFIG_DIR, Settings, load_settings

FIXTURES = Path(__file__).parent / "fixtures"

BASE_TOML = """
log_dir = "{log_dir}"
data_dir = "{data_dir}"
[server]
host = "127.0.0.1"
port = 8765
[ollama]
base_url = "{base_url}"
model = "test-model:latest"
num_ctx = 8192
allowed_num_ctx = [4096, 8192]
connect_timeout_s = 1.0
request_timeout_s = 10.0
[permissions]
global_grants = []
[[permissions.roots]]
id = "sandbox"
path = "{root}"
grants = ["fs.read"]
[capabilities]
list_files_root = "sandbox"
[plugins]
dir = "{plugins}"
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    root = tmp_path / "sandbox"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("hello", encoding="utf-8")
    (root / "sub" / "b.txt").write_text("world", encoding="utf-8")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.txt").write_text("do not list", encoding="utf-8")
    (tmp_path / "data").mkdir()
    return root


@pytest.fixture
def make_settings(tmp_path: Path, sandbox: Path):
    def _make(extra: str = "", base_url: str = "http://127.0.0.1:11434") -> Settings:
        path = tmp_path / "test.toml"
        text = BASE_TOML.format(
            log_dir=(tmp_path / "logs").as_posix(),
            data_dir=(tmp_path / "data").as_posix(),
            base_url=base_url,
            root=sandbox.as_posix(),
            plugins=(tmp_path / "plugins").as_posix(),
        )
        path.write_text(text + extra, encoding="utf-8")
        return load_settings([path])

    return _make


def _ollama_up() -> bool:
    try:
        return httpx.get("http://127.0.0.1:11434/api/version", timeout=1.0, trust_env=False).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture
def real_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    if not _ollama_up():
        pytest.skip("Ollama is not running on 127.0.0.1:11434 (start scripts/start-ollama.ps1)")
    monkeypatch.setenv("XODUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XODUS_LOG_DIR", str(tmp_path / "logs"))
    return load_settings([CONFIG_DIR / "default.toml"])
