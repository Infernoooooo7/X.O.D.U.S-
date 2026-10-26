"""Ollama adapter behind the provider interface: capability confirmation, budgets, structured output,
tool calls, thinking gating, embeddings, load/unload, and error mapping. Uses a mock transport."""

from __future__ import annotations

import json

import httpx
import pytest

from tests.conftest import free_port
from xodus.providers import build_provider
from xodus.providers.base import (
    CapabilityNotConfirmed,
    InputTooLarge,
    InvalidStructuredOutput,
    ModelNotAvailable,
    ProviderUnavailable,
    RequestBudget,
    ToolSpec,
)
from xodus.providers.ollama import OllamaProvider

BUDGET = RequestBudget(num_ctx=4096, max_output_tokens=256, timeout_s=5)


class FakeOllama:
    def __init__(self, capabilities=("completion", "tools"), chat=None, status=200):
        self.capabilities = list(capabilities)
        self.chat = chat or {"message": {"content": "hello"}, "done": True, "prompt_eval_count": 12, "eval_count": 3}
        self.status = status
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.url.path, body))
        path = request.url.path
        if path == "/api/show":
            return httpx.Response(200, json={"capabilities": self.capabilities, "details": {"family": "qwen3",
                                  "quantization_level": "Q4_K_M", "parameter_size": "4.0B"},
                                  "model_info": {"qwen3.context_length": 262144}})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "test-model:latest", "digest": "abc", "size": 1}]})
        if path == "/api/version":
            return httpx.Response(200, json={"version": "0.0-test"})
        if path == "/api/chat":
            return httpx.Response(self.status, json=self.chat if self.status == 200 else {"error": "boom"})
        if path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [[0.1, 0.2]] * len(body["input"])})
        if path == "/api/generate":
            return httpx.Response(200, json={"done": True})
        return httpx.Response(404, json={"error": "not found"})

    def chats(self):
        return [b for p, b in self.requests if p == "/api/chat"]


def provider(settings, fake, **kw):
    return OllamaProvider(settings.ollama, transport=httpx.MockTransport(fake), **kw)


async def test_probe_confirms_only_declared_and_reported_capabilities(make_settings):
    p = provider(make_settings(), FakeOllama(capabilities=["completion", "vision"]))
    info = await p.probe()
    assert info.reported_capabilities == ["completion", "vision"]
    assert info.declared == ["completion", "structured", "tools"]
    assert info.confirmed == ["completion", "structured"]  # tools declared but not reported; vision not declared
    assert info.context_length == 262144 and info.quantization == "Q4_K_M"


async def test_unconfirmed_capability_is_refused_without_calling_the_model(make_settings):
    fake = FakeOllama(capabilities=["completion"])
    p = provider(make_settings(), fake)
    with pytest.raises(CapabilityNotConfirmed, match="tools"):
        await p.chat_with_tools([{"role": "user", "content": "x"}], [ToolSpec("t", "d", {"type": "object"})], BUDGET)
    assert fake.chats() == []


async def test_thinking_is_never_requested_unless_confirmed(make_settings):
    fake = FakeOllama(capabilities=["completion", "tools"])
    p = provider(make_settings(extra='\n[ollama.sampling]\ntemperature = 0.7\n'), fake)
    await p.generate([{"role": "user", "content": "x"}], BUDGET)
    assert "think" not in fake.chats()[0]  # model without thinking: the field is omitted
    with pytest.raises(CapabilityNotConfirmed, match="thinking"):
        await p.generate([{"role": "user", "content": "x"}], RequestBudget(4096, 256, thinking=True))


async def test_thinking_models_get_think_false_explicitly(make_settings, monkeypatch):
    monkeypatch.setenv("XODUS_OLLAMA__CAPABILITIES", '["completion", "structured", "tools", "thinking"]')
    fake = FakeOllama(capabilities=["completion", "tools", "thinking"])
    p = provider(make_settings(), fake)
    await p.generate([{"role": "user", "content": "x"}], BUDGET)
    assert fake.chats()[0]["think"] is False
    await p.generate([{"role": "user", "content": "x"}], RequestBudget(4096, 256, thinking=True))
    assert fake.chats()[1]["think"] is True


async def test_every_request_sets_num_ctx_and_output_limit(make_settings):
    fake = FakeOllama()
    p = provider(make_settings(), fake)
    completion = await p.generate([{"role": "user", "content": "x"}], BUDGET, seed=7)
    options = fake.chats()[0]["options"]
    assert options["num_ctx"] == 4096 and options["num_predict"] == 256 and options["seed"] == 7
    assert completion.usage.prompt_tokens == 12 and completion.usage.output_tokens == 3


async def test_oversized_input_is_refused_before_sending(make_settings):
    fake = FakeOllama()
    p = provider(make_settings(), fake)
    with pytest.raises(InputTooLarge, match="limit is 3840"):
        await p.generate([{"role": "user", "content": "x" * 20_000}], BUDGET)
    assert fake.chats() == []


def test_request_budget_validation():
    with pytest.raises(ValueError):
        RequestBudget(num_ctx=1024, max_output_tokens=1024)
    assert RequestBudget(num_ctx=4096, max_output_tokens=96, max_input_tokens=100).input_limit() == 100


async def test_structured_output_is_validated(make_settings):
    schema = {"type": "object", "additionalProperties": False, "required": ["n"], "properties": {"n": {"type": "integer"}}}
    good = FakeOllama(chat={"message": {"content": '{"n": 3}'}, "done": True})
    data, _ = await provider(make_settings(), good).structured([{"role": "user", "content": "x"}], schema, BUDGET)
    assert data == {"n": 3} and good.chats()[0]["format"] == schema
    for content in ('{"n": "three"}', '{"n": 3, "extra": 1}', "not json"):
        bad = FakeOllama(chat={"message": {"content": content}, "done": True})
        with pytest.raises(InvalidStructuredOutput):
            await provider(make_settings(), bad).structured([{"role": "user", "content": "x"}], schema, BUDGET)


async def test_tool_calls_are_parsed_as_untrusted_data(make_settings):
    calls = [{"function": {"name": "fs__list_files", "arguments": {"path": "sub"}}},
             {"function": {"name": "fs__list_files", "arguments": '{"path": "x"}'}},
             {"function": {"name": "odd", "arguments": "not json"}}]
    fake = FakeOllama(chat={"message": {"content": "", "tool_calls": calls}, "done": True})
    tools = [ToolSpec("fs__list_files", "List files", {"type": "object"})]
    completion = await provider(make_settings(), fake).chat_with_tools([{"role": "user", "content": "x"}], tools, BUDGET)
    assert [(c.name, c.arguments) for c in completion.tool_calls] == [
        ("fs__list_files", {"path": "sub"}), ("fs__list_files", {"path": "x"}), ("odd", "not json")]
    assert fake.chats()[0]["tools"][0]["function"]["name"] == "fs__list_files"


async def test_embed_load_unload_and_events(make_settings):
    fake = FakeOllama()
    events = []
    p = provider(make_settings(), fake, on_event=lambda event, **f: events.append(event))
    assert await p.embed(["a", "b"]) == [[0.1, 0.2], [0.1, 0.2]]
    embed_body = [b for path, b in fake.requests if path == "/api/embed"][0]
    assert embed_body["model"] == "qwen3-embedding:0.6b" and embed_body["truncate"] is False
    await p.load(num_ctx=4096)
    await p.unload()
    generate = [b for path, b in fake.requests if path == "/api/generate"]
    assert generate[0]["options"]["num_ctx"] == 4096 and generate[1]["keep_alive"] == 0
    assert events == ["model_loaded", "model_unloaded"]


async def test_error_mapping(make_settings):
    with pytest.raises(ModelNotAvailable):
        await provider(make_settings(), FakeOllama(status=404)).generate([{"role": "user", "content": "x"}], BUDGET)
    down = OllamaProvider(make_settings(base_url=f"http://127.0.0.1:{free_port()}").ollama)
    with pytest.raises(ProviderUnavailable, match="still running"):
        await down.generate([{"role": "user", "content": "x"}], BUDGET)
    health = await down.health()
    assert health["status"] == "unreachable"


def test_provider_is_chosen_by_config(make_settings):
    assert isinstance(build_provider(make_settings()), OllamaProvider)
