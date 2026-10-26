"""Typed configuration loaded from config/default.toml, config/local.toml and XODUS_* env vars.

Precedence (highest first): environment variables, local.toml, default.toml.
Validation failures raise ConfigError with one line per problem.
"""

from __future__ import annotations

import ipaddress
import tomllib
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"


class ConfigError(Exception):
    """Configuration is missing or invalid; the message is meant for the user."""


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


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
        if not _is_loopback(v):
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
    num_ctx: int = Field(8192, ge=512, le=16384)
    allowed_num_ctx: list[int] = [4096, 8192, 16384]
    max_output_tokens: int = Field(1024, ge=1, le=8192)
    keep_alive: str = "10m"
    connect_timeout_s: float = Field(3.0, gt=0)
    request_timeout_s: float = Field(120.0, gt=0)
    sampling: SamplingSettings = SamplingSettings()

    @field_validator("base_url")
    @classmethod
    def _loopback_url(cls, v: str) -> str:
        parsed = urlparse(v)
        if parsed.scheme != "http" or not parsed.hostname:
            raise ValueError(f"must be an http:// URL, got {v!r}")
        if not _is_loopback(parsed.hostname):
            raise ValueError(f"must point at a loopback host, got {parsed.hostname!r}")
        return v.rstrip("/")

    @model_validator(mode="after")
    def _default_ctx_allowed(self) -> OllamaSettings:
        if self.num_ctx not in self.allowed_num_ctx:
            raise ValueError(f"num_ctx {self.num_ctx} is not in allowed_num_ctx {self.allowed_num_ctx}")
        return self


class ChatSettings(_Section):
    system_prompt: str = ""
    max_message_chars: int = Field(8000, ge=1, le=100_000)


class EnvelopeSettings(_Section):
    max_running: int = Field(1, ge=1, le=1)  # OLLAMA_NUM_PARALLEL=1, so more would only queue in Ollama
    max_queued: int = Field(4, ge=0, le=64)


class CapabilitySettings(_Section):
    list_files_root: Path
    list_files_max_entries: int = Field(500, ge=1, le=10_000)

    @field_validator("list_files_root")
    @classmethod
    def _root_is_dir(cls, v: Path) -> Path:
        path = _repo_path(v)
        if not path.is_dir():
            raise ValueError(f"folder does not exist: {path}")
        return path.resolve()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="XODUS_", env_nested_delimiter="__", extra="forbid")

    _file_data: ClassVar[dict[str, Any]] = {}

    log_dir: Path = Path("logs")
    server: ServerSettings = ServerSettings()
    ollama: OllamaSettings
    chat: ChatSettings = ChatSettings()
    envelope: EnvelopeSettings = EnvelopeSettings()
    capabilities: CapabilitySettings

    @field_validator("log_dir")
    @classmethod
    def _resolve_log_dir(cls, v: Path) -> Path:
        return _repo_path(v)

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
