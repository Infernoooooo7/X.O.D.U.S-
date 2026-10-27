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


@pytest.fixture(autouse=True)
def _ollama_server_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake Ollama servers listen inside this pytest process, so /api/health's Ollama environment check reads this
    process's environment: give it what scripts/start-ollama.ps1 sets. Tests that need a bad setting change it."""
    monkeypatch.setenv("OLLAMA_NO_CLOUD", "1")
    monkeypatch.delenv("OLLAMA_REMOTES", raising=False)
    monkeypatch.delenv("OLLAMA_ORIGINS", raising=False)


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


# ---------------------------------------------------------------------------------------------- knowledge fixtures

KB_TOML = """
[embedding]
dimensions = {dim}
num_gpu = 0
batch_size = 4
query_template = "{{text}}"
document_template = "{{text}}"
[knowledge]
min_term_match_ratio = 0.5
[ingestion]
worker_idle_timeout_s = 60
poll_interval_s = 0.05
embed_retries = 1
embed_retry_backoff_s = 0.05
watchdog_s = 30
[[permissions.roots]]
id = "docs"
path = "{docs}"
grants = ["fs.read"]
"""


@pytest.fixture
def docs_root(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    root.mkdir()
    return root


@pytest.fixture
def kb_toml(docs_root: Path) -> str:
    from tests.kb_helpers import DIM

    return KB_TOML.format(dim=DIM, docs=docs_root.as_posix())


@pytest.fixture
def kb_settings(make_settings, kb_toml):
    def _make(extra: str = "", base_url: str = "http://127.0.0.1:11434") -> Settings:
        return make_settings(extra=kb_toml + extra, base_url=base_url)

    return _make


@pytest.fixture
def fake_ollama():
    from tests.kb_helpers import FakeOllama

    server = FakeOllama()
    yield server
    server.stop()


@pytest.fixture
def kb_store(kb_settings):
    """An opened, migrated store over a temporary database (in-process, no worker)."""
    from xodus.storage.knowledge import KnowledgeStore
    from xodus.storage.sqlite import Database

    settings = kb_settings()
    db = Database(settings.data_dir / settings.storage.filename, embedding_dim=settings.embedding.dimensions)
    db.open()
    store = KnowledgeStore(db, max_chunks=settings.knowledge.max_chunks)
    store.settings = settings  # convenience for tests
    yield store
    db.close()
