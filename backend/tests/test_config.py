from __future__ import annotations

import pytest

from xodus.config.settings import CONFIG_DIR, ConfigError, load_settings


def test_checked_in_default_config_is_valid():
    settings = load_settings([CONFIG_DIR / "default.toml"])
    assert settings.server.host == "127.0.0.1"
    assert settings.ollama.num_ctx in settings.ollama.allowed_num_ctx
    roots = {r.id: r for r in settings.permissions.roots}
    assert roots[settings.capabilities.list_files_root].path.is_dir()
    assert roots["sandbox"].grants == ["fs.read"]  # read-only by default
    assert settings.permissions.global_grants == []  # no network or process-spawning grants by default
    assert settings.executor.default_arm == "react"  # H3 control = baseline B0
    assert settings.executor.plan_execute.max_replans == 1


def test_valid_test_config_loads(make_settings):
    settings = make_settings()
    assert settings.ollama.model == "test-model:latest"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com", "::"])
def test_non_loopback_server_host_fails_with_clear_message(tmp_path, make_settings, host):
    make_settings()  # writes test.toml
    path = tmp_path / "test.toml"
    path.write_text(path.read_text().replace('host = "127.0.0.1"', f'host = "{host}"'), encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_settings([path])
    assert "server.host" in str(info.value) and "loopback" in str(info.value)


def test_remote_ollama_url_is_rejected(make_settings):
    with pytest.raises(ConfigError, match="ollama.base_url"):
        make_settings(base_url="http://10.0.0.5:11434")


def test_default_num_ctx_must_be_allowed(make_settings, tmp_path):
    make_settings()
    path = tmp_path / "test.toml"
    path.write_text(path.read_text().replace("num_ctx = 8192", "num_ctx = 2048"), encoding="utf-8")
    with pytest.raises(ConfigError, match="allowed_num_ctx"):
        load_settings([path])


def test_missing_capability_root_is_rejected(make_settings, tmp_path):
    make_settings()
    path = tmp_path / "test.toml"
    text = path.read_text()
    path.write_text(text.replace("sandbox", "does-not-exist"), encoding="utf-8")
    with pytest.raises(ConfigError, match="folder does not exist"):
        load_settings([path])


def test_unknown_keys_are_rejected(make_settings):
    with pytest.raises(ConfigError, match="Extra inputs"):
        make_settings(extra="\n[ollama.sampling]\nbogus = 1\n")


def test_env_overrides_file(make_settings, monkeypatch):
    monkeypatch.setenv("XODUS_OLLAMA__MODEL", "env-model:1b")
    assert make_settings().ollama.model == "env-model:1b"


@pytest.mark.parametrize(
    "extra, match",
    [
        ('\n[executor.plan_execute]\nmax_steps = 6\nmax_model_calls = 4\nmax_total_tokens = 32000\nmax_wall_s = 300\nmax_replans = 2\n', "max_replans"),
        ('\n[permissions.extra]\n', "Extra inputs"),
        ('\n[executor]\ndefault_arm = "yolo"\n', "default_arm"),
        ('\n[[permissions.roots]]\nid = "sandbox"\npath = "."\n', "root ids must be unique"),
        ('\n[ollama.sampling]\ntemperature = 9\n', "temperature"),
    ],
)
def test_more_invalid_settings(make_settings, extra, match):
    with pytest.raises(ConfigError, match=match):
        make_settings(extra=extra)


def test_global_grants_vocabulary_is_closed(make_settings, monkeypatch):
    monkeypatch.setenv("XODUS_PERMISSIONS__GLOBAL_GRANTS", '["fs.write:everything"]')
    with pytest.raises(ConfigError, match="global_grants"):
        make_settings()


def test_invalid_toml_is_reported(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("[server\nhost=", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_settings([bad])
