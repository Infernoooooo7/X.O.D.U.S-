"""Provider-neutral model interface (master prompt 4.1).

Only adapters know a provider's wire API. Every call carries a RequestBudget (context size, input and
output token limits, thinking on/off, timeout). Model capabilities are declared in config and must be
confirmed by the adapter's probe before use; an unconfirmed capability is refused, never assumed.
Cancellation is asyncio task cancellation, which closes the underlying HTTP request.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

MODEL_CAPABILITIES = {"completion", "structured", "tools", "thinking", "embedding", "vision"}


class ProviderError(Exception):
    code = "provider_error"

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class ProviderUnavailable(ProviderError):
    code = "provider_unavailable"


class ModelNotAvailable(ProviderError):
    code = "model_not_available"


class ProviderTimeout(ProviderError):
    code = "provider_timeout"


class CapabilityNotConfirmed(ProviderError):
    code = "model_capability_unconfirmed"


class InputTooLarge(ProviderError):
    code = "input_too_large"


class InvalidStructuredOutput(ProviderError):
    code = "invalid_structured_output"


@dataclass(frozen=True)
class RequestBudget:
    num_ctx: int
    max_output_tokens: int
    max_input_tokens: int | None = None  # default: num_ctx - max_output_tokens
    thinking: bool = False
    timeout_s: float = 120.0

    def __post_init__(self) -> None:
        if not 0 < self.max_output_tokens < self.num_ctx:
            raise ValueError("max_output_tokens must be positive and smaller than num_ctx")
        if self.timeout_s <= 0:
            raise ValueError("timeout_s must be positive")

    def input_limit(self) -> int:
        limit = self.num_ctx - self.max_output_tokens
        return min(limit, self.max_input_tokens) if self.max_input_tokens is not None else limit


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Any  # untrusted: whatever the model produced


@dataclass
class Usage:
    prompt_tokens: int = 0
    output_tokens: int = 0
    total_ms: float = 0.0
    load_ms: float = 0.0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.output_tokens


@dataclass
class Completion:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    thinking: str | None = None
    done_reason: str | None = None


@dataclass(frozen=True)
class StreamChunk:
    text: str = ""
    final: dict[str, Any] | None = None  # provider-specific final record (kept for logging)
    usage: Usage | None = None  # set on the final chunk


@dataclass
class ModelInfo:
    name: str
    digest: str | None
    context_length: int | None
    quantization: str | None
    family: str | None
    parameter_size: str | None
    reported_capabilities: list[str]
    declared: list[str]
    confirmed: list[str]

    def public(self) -> dict[str, Any]:
        return asdict(self)


class ModelProvider(Protocol):
    name: str

    @property
    def model(self) -> str: ...

    async def health(self) -> dict[str, Any]: ...

    async def list_models(self) -> list[dict[str, Any]]: ...

    async def model_info(self, model: str | None = None) -> ModelInfo: ...

    async def probe(self) -> ModelInfo: ...

    def confirmed_capabilities(self) -> set[str] | None: ...

    async def generate(self, messages: list[dict[str, Any]], budget: RequestBudget, *, seed: int | None = None) -> Completion: ...

    def stream(self, messages: list[dict[str, Any]], budget: RequestBudget, *, seed: int | None = None) -> AsyncIterator[StreamChunk]: ...

    async def structured(self, messages: list[dict[str, Any]], schema: dict[str, Any], budget: RequestBudget, *,
                         seed: int | None = None) -> tuple[Any, Completion]: ...

    async def chat_with_tools(self, messages: list[dict[str, Any]], tools: list[ToolSpec], budget: RequestBudget, *,
                              seed: int | None = None) -> Completion: ...

    async def embed(self, texts: list[str], *, timeout_s: float | None = None, dimensions: int | None = None) -> list[list[float]]: ...

    async def load(self, *, num_ctx: int) -> None: ...

    async def unload(self) -> None: ...

    async def running_models(self) -> list[dict[str, Any]]: ...

    async def aclose(self) -> None: ...


def estimate_tokens(messages: list[dict[str, Any]], tools: list[ToolSpec] | None = None) -> int:
    """Conservative pre-flight estimate: about 3 characters per token plus per-message overhead.

    Ollama has no tokenize endpoint, so this deliberately over-estimates; the actual counts come back
    in the response (prompt_eval_count) and are what budgets record.
    """
    chars = sum(len(str(m.get("content") or "")) + len(json.dumps(m.get("tool_calls") or "")) for m in messages)
    if tools:
        chars += len(json.dumps([asdict(t) for t in tools]))
    return chars // 3 + 8 * len(messages) + 16
