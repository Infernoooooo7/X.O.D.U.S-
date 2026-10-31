"""Typed configuration loaded from config/default.toml, config/local.toml and XODUS_* env vars.

Precedence (highest first): environment variables, local.toml, default.toml.
Validation failures raise ConfigError with one line per problem.
"""

from __future__ import annotations

import ipaddress
import tomllib
from pathlib import Path
from typing import Any, ClassVar, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"

ModelCapability = Literal["completion", "structured", "tools", "thinking", "embedding", "vision"]


class ConfigError(Exception):
    """Configuration is missing or invalid; the message is meant for the user."""


def is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def is_cloud_model_name(name: str) -> bool:
    """Ollama marks models that run on ollama.com, not on this computer, with a '-cloud' tag (e.g. 'gpt-oss:120b-cloud');
    the suffix is present in the installed Ollama 0.40.11 binary. XODUS only uses local open-weight models."""
    tag = name.rsplit(":", 1)[1] if ":" in name else name
    return tag.lower() == "cloud" or tag.lower().endswith("-cloud")


def _local_model(name: str) -> str:
    if is_cloud_model_name(name):
        raise ValueError(f"{name!r} is an Ollama cloud model (it runs on ollama.com, not on this computer); "
                         "XODUS uses local models only")
    return name


def _repo_path(value: Path | str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerSettings(_Section):
    host: str = "127.0.0.1"
    port: int = Field(8765, ge=1024, le=65535)

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        if not is_loopback_host(v):
            raise ValueError(f"must be a loopback address (127.0.0.1, ::1 or localhost), got {v!r}")
        return v


class SamplingSettings(_Section):
    temperature: float = Field(0.7, ge=0.0, le=2.0)
    top_p: float = Field(0.8, gt=0.0, le=1.0)
    top_k: int = Field(20, ge=1)


class OllamaSettings(_Section):
    base_url: str = "http://127.0.0.1:11434"
    model: str = Field(min_length=1)
    expected_digest_prefix: str | None = None
    capabilities: list[ModelCapability] = ["completion", "structured", "tools"]
    embed_model: str = "qwen3-embedding:0.6b"
    num_ctx: int = Field(8192, ge=512, le=16384)
    allowed_num_ctx: list[int] = [4096, 8192, 16384]
    max_output_tokens: int = Field(1024, ge=1, le=8192)
    keep_alive: str = "10m"
    connect_timeout_s: float = Field(3.0, gt=0)
    request_timeout_s: float = Field(120.0, gt=0)
    sampling: SamplingSettings = SamplingSettings()

    @field_validator("model", "embed_model")
    @classmethod
    def _local_models(cls, v: str) -> str:
        return _local_model(v)

    @field_validator("base_url")
    @classmethod
    def _loopback_url(cls, v: str) -> str:
        parsed = urlparse(v)
        if parsed.scheme != "http" or not parsed.hostname:
            raise ValueError(f"must be an http:// URL, got {v!r}")
        if not is_loopback_host(parsed.hostname):
            raise ValueError(f"must point at a loopback host, got {parsed.hostname!r}")
        return v.rstrip("/")

    @model_validator(mode="after")
    def _default_ctx_allowed(self) -> OllamaSettings:
        if self.num_ctx not in self.allowed_num_ctx:
            raise ValueError(f"num_ctx {self.num_ctx} is not in allowed_num_ctx {self.allowed_num_ctx}")
        if self.max_output_tokens >= min(self.allowed_num_ctx):
            raise ValueError("max_output_tokens must be smaller than every allowed num_ctx")
        return self


class ChatSettings(_Section):
    system_prompt: str = ""
    max_message_chars: int = Field(8000, ge=1, le=100_000)
    max_history_tokens: int = Field(3000, ge=0, le=16_384)  # earlier turns of a conversation; oldest dropped first


class ModelProfileSettings(_Section):
    """A switchable generation profile. The 'default' profile is not listed: it is the [ollama] section itself."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    label: str = Field("", max_length=80)
    model: str = Field(min_length=1, max_length=200)
    expected_digest_prefix: str | None = None
    capabilities: list[ModelCapability] = ["completion", "structured", "tools"]
    num_ctx: int = Field(8192, ge=512, le=16384)
    enabled: bool = True  # false = listed for information, never switched to (e.g. does not fit in VRAM)
    note: str = Field("", max_length=300)

    @field_validator("model")
    @classmethod
    def _local_models(cls, v: str) -> str:
        return _local_model(v)


class ModelSettings(_Section):
    profiles: list[ModelProfileSettings] = []
    unload_wait_s: float = Field(15.0, gt=0, le=120)  # how long to wait for Ollama to drop the old model
    warmup: bool = True  # one short generation after loading, so the first real request does not pay for it


class ThresholdSettings(_Section):
    """Master prompt 3.7 (TARGET values). GB means 10^9 bytes. Enforced by the resource governor (Gate F). The USS check
    adds the shared GPU memory of XODUS processes; the VRAM check is XODUS's own dedicated GPU memory (user decision,
    29 Oct 2026), not device-wide use. See xodus/resources/thresholds.py."""

    warn_uss_gb: float = Field(3.6, gt=0)
    stop_uss_gb: float = Field(5.5, gt=0)
    warn_vram_gb: float = Field(4.2, gt=0)
    stop_vram_gb: float = Field(4.8, gt=0)
    stop_available_gb: float = Field(1.5, ge=0)
    recover_after_s: float = Field(30.0, gt=0)
    enforce: bool = True  # false: levels are shown only, nothing is paused or refused
    check_interval_s: float = Field(2.0, ge=0.1, le=60)  # one sampler snapshot (~60-75 ms of one thread) per check
    idle_grace_s: float = Field(10.0, ge=0)  # an optional process unused this long counts as idle at the warn level

    @model_validator(mode="after")
    def _ordered(self) -> ThresholdSettings:
        if self.warn_uss_gb >= self.stop_uss_gb or self.warn_vram_gb >= self.stop_vram_gb:
            raise ValueError("each warn level must be below its stop level")
        return self


class EnvelopeSettings(_Section):
    max_running: int = Field(1, ge=1, le=1)  # OLLAMA_NUM_PARALLEL=1, so more would only queue in Ollama
    max_queued: int = Field(4, ge=0, le=64)


class RootSettings(_Section):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    path: Path
    grants: list[Literal["fs.read", "fs.write"]] = ["fs.read"]

    @field_validator("path")
    @classmethod
    def _existing_folder(cls, v: Path) -> Path:
        path = _repo_path(v)
        if path.is_symlink() or (path.exists() and path.is_junction()):
            raise ValueError(f"root must not be a link: {path}")
        if not path.is_dir():
            raise ValueError(f"folder does not exist: {path}")
        return path.resolve()


class CollectionGrantSettings(_Section):
    """What a model-invoked capability may do with the knowledge collection of this name (deny by default)."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    grants: list[Literal["kb.read"]] = ["kb.read"]


class PermissionSettings(_Section):
    global_grants: list[Literal["net.outbound", "exec.spawn"]] = []
    roots: list[RootSettings] = []
    collections: list[CollectionGrantSettings] = []

    @model_validator(mode="after")
    def _unique_ids(self) -> PermissionSettings:
        ids = [r.id for r in self.roots]
        if len(ids) != len(set(ids)):
            raise ValueError("root ids must be unique")
        names = [c.id for c in self.collections]
        if len(names) != len(set(names)):
            raise ValueError("collection grant ids must be unique")
        return self


class StorageSettings(_Section):
    filename: str = Field("xodus.db", pattern=r"^[A-Za-z0-9._-]{1,64}\.db$")  # inside data_dir
    cache_kib: int = Field(16384, ge=1024, le=262_144)  # SQLite page cache per connection
    busy_timeout_ms: int = Field(10_000, ge=100, le=120_000)
    pool_size: int = Field(4, ge=1, le=5)  # brief: connection pool <= 5


class EmbeddingSettings(_Section):
    dimensions: int = Field(1024, ge=32, le=4096)  # must equal the vector column; checked at startup
    num_gpu: int = Field(0, ge=-1)  # 0 = embedder on the CPU (provisional; CPU vs GPU is a Gate G experiment); -1 = Ollama decides
    query_template: str = "Instruct: Given a question, retrieve the passages that answer it.\nQuery: {text}"
    document_template: str = "{text}"
    batch_size: int = Field(16, ge=1, le=16)  # envelope: embedding batches <= 16 chunks
    timeout_s: float = Field(120.0, gt=0, le=600)

    @field_validator("query_template", "document_template")
    @classmethod
    def _has_slot(cls, v: str) -> str:
        if v.count("{text}") != 1:
            raise ValueError("template must contain {text} exactly once")
        return v


class KnowledgeSettings(_Section):
    # supported workload envelope (master prompt 3.5)
    max_file_mb: int = Field(50, ge=1, le=50)
    max_pdf_pages: int = Field(500, ge=1, le=500)
    max_chunks: int = Field(100_000, ge=1, le=100_000)
    max_files_per_request: int = Field(200, ge=1, le=1000)
    # untrusted-file defences
    max_docx_uncompressed_mb: int = Field(200, ge=1, le=1024)
    max_zip_ratio: int = Field(150, ge=2, le=10_000)
    max_zip_entries: int = Field(5000, ge=10, le=100_000)
    max_csv_field_kb: int = Field(1024, ge=1, le=16_384)
    # chunking: tokens are estimated as characters / chars_per_token (no tokenizer is available)
    chars_per_token: float = Field(4.0, ge=1.0, le=8.0)
    chunk_min_tokens: int = Field(400, ge=50)
    chunk_target_tokens: int = Field(500, ge=50)
    chunk_max_tokens: int = Field(600, ge=50)
    chunk_overlap_tokens: int = Field(50, ge=0, le=200)
    # retrieval
    k_vector: int = Field(40, ge=1, le=200)
    k_fts: int = Field(40, ge=1, le=200)
    rrf_k: int = Field(60, ge=1, le=1000)
    default_k: int = Field(8, ge=1, le=20)
    max_k: int = Field(20, ge=1, le=50)
    evidence_token_budget: int = Field(2400, ge=200, le=6000)
    max_query_chars: int = Field(500, ge=10, le=4000)
    # "is the evidence sufficient": provisional heuristics, calibrated in this gate on a small corpus only
    min_vector_similarity: float = Field(0.0, ge=0.0, le=1.0)  # 0 disables the vector test
    min_term_match_ratio: float = Field(0.5, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _ordered(self) -> KnowledgeSettings:
        if not self.chunk_min_tokens <= self.chunk_target_tokens <= self.chunk_max_tokens:
            raise ValueError("chunk_min_tokens <= chunk_target_tokens <= chunk_max_tokens must hold")
        if self.default_k > self.max_k:
            raise ValueError("default_k must not exceed max_k")
        return self


class IngestionSettings(_Section):
    worker_idle_timeout_s: float = Field(60.0, gt=0)  # backend stops the idle worker (brief section 2)
    stall_timeout_s: float = Field(300.0, gt=0)  # backend kills a worker whose job heartbeat is older than this
    watchdog_s: float = Field(180.0, gt=0)  # the worker ends itself if it makes no progress this long (hung parser)
    max_attempts: int = Field(3, ge=1, le=10)  # a job that kills its worker this many times is failed
    worker_memory_limit_mb: int | None = Field(1536, ge=64)  # Job Object safety net; the target is 400 MB peak
    poll_interval_s: float = Field(1.0, gt=0)
    monitor_interval_s: float = Field(1.0, gt=0)
    max_restarts_per_minute: int = Field(5, ge=1)
    embed_retries: int = Field(3, ge=0, le=10)
    embed_retry_backoff_s: float = Field(1.0, ge=0)


class CapabilitySettings(_Section):
    list_files_root: str = "sandbox"  # id of a [[permissions.roots]] entry
    list_files_max_entries: int = Field(500, ge=1, le=10_000)
    disabled: list[str] = []


class ArmBudgetSettings(_Section):
    max_steps: int = Field(ge=1, le=20)
    max_model_calls: int = Field(ge=1, le=40)
    max_total_tokens: int = Field(ge=1000, le=1_000_000)
    max_wall_s: float = Field(gt=0, le=3600)
    max_replans: int = Field(0, ge=0, le=1)  # the brief allows at most one re-plan


class ExecutorSettings(_Section):
    default_arm: Literal["react", "plan_execute"] = "react"  # react = H3 control = baseline B0
    max_active_tasks: int = Field(2, ge=1, le=8)
    max_tool_output_bytes: int = Field(8000, ge=256, le=100_000)
    seed: int | None = None
    react: ArmBudgetSettings = ArmBudgetSettings(max_steps=10, max_model_calls=11, max_total_tokens=120_000, max_wall_s=600)
    plan_execute: ArmBudgetSettings = ArmBudgetSettings(max_steps=6, max_model_calls=4, max_total_tokens=32_000,
                                                        max_wall_s=300, max_replans=1)


class ApprovalSettings(_Section):
    ttl_s: float = Field(600, ge=0.05, le=86_400)


class SupervisorSettings(_Section):
    max_optional_processes: int = Field(2, ge=0, le=16)
    optional_idle_timeout_s: float = Field(300, gt=0)
    optional_memory_limit_mb: int | None = Field(512, ge=32)
    reaper_interval_s: float = Field(5.0, gt=0)


class PluginSettings(_Section):
    dir: Path = Path("plugins")
    trusted_entry_point_distributions: list[str] = []
    approved_secrets: dict[str, list[str]] = {}  # capability id -> secret names it may receive

    @field_validator("dir")
    @classmethod
    def _resolve(cls, v: Path) -> Path:
        return _repo_path(v)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="XODUS_", env_nested_delimiter="__", extra="forbid")

    _file_data: ClassVar[dict[str, Any]] = {}

    log_dir: Path = Path("logs")
    data_dir: Path = Path("data")
    provider: Literal["ollama"] = "ollama"
    server: ServerSettings = ServerSettings()
    ollama: OllamaSettings
    chat: ChatSettings = ChatSettings()
    envelope: EnvelopeSettings = EnvelopeSettings()
    permissions: PermissionSettings = PermissionSettings()
    capabilities: CapabilitySettings = CapabilitySettings()
    storage: StorageSettings = StorageSettings()
    embedding: EmbeddingSettings = EmbeddingSettings()
    knowledge: KnowledgeSettings = KnowledgeSettings()
    ingestion: IngestionSettings = IngestionSettings()
    executor: ExecutorSettings = ExecutorSettings()
    approvals: ApprovalSettings = ApprovalSettings()
    supervisor: SupervisorSettings = SupervisorSettings()
    plugins: PluginSettings = PluginSettings()
    models: ModelSettings = ModelSettings()
    thresholds: ThresholdSettings = ThresholdSettings()

    @field_validator("log_dir", "data_dir")
    @classmethod
    def _resolve_dir(cls, v: Path) -> Path:
        return _repo_path(v)

    @model_validator(mode="after")
    def _profiles(self) -> Settings:
        ids = [p.id for p in self.models.profiles]
        if "default" in ids:
            raise ValueError("models.profiles: 'default' is reserved (the default profile is the [ollama] section)")
        if len(ids) != len(set(ids)):
            raise ValueError("models.profiles: profile ids must be unique")
        for p in self.models.profiles:
            if p.num_ctx not in self.ollama.allowed_num_ctx:
                raise ValueError(f"models.profiles.{p.id}: num_ctx {p.num_ctx} is not in ollama.allowed_num_ctx")
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return init_settings, env_settings, _DictSource(settings_cls, settings_cls._file_data)


class _DictSource(PydanticBaseSettingsSource):
    def __init__(self, settings_cls: type[BaseSettings], data: dict[str, Any]) -> None:
        super().__init__(settings_cls)
        self._data = data

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._data)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def default_config_files() -> list[Path]:
    return [p for p in (CONFIG_DIR / "default.toml", CONFIG_DIR / "local.toml") if p.exists()]


def load_settings(files: list[Path] | None = None) -> Settings:
    files = default_config_files() if files is None else files
    if not files:
        raise ConfigError(f"No configuration file found in {CONFIG_DIR}")
    data: dict[str, Any] = {}
    for path in files:
        try:
            _deep_merge(data, tomllib.loads(path.read_text(encoding="utf-8")))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: invalid TOML: {exc}") from exc

    loaded = type("LoadedSettings", (Settings,), {"_file_data": data})
    try:
        return loaded()
    except ValidationError as exc:
        lines = [f"Invalid configuration ({', '.join(str(p) for p in files)}):"]
        for err in exc.errors():
            loc = ".".join(str(part) for part in err["loc"])
            lines.append(f"  - {loc}: {err['msg']}")
        raise ConfigError("\n".join(lines)) from exc
