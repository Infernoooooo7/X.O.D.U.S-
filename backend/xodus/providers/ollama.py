"""Ollama adapter: the only module that knows Ollama's HTTP API (/api/chat, /api/embed, /api/show,
/api/tags, /api/ps, /api/generate for load/unload, format, tools, think, keep_alive, options).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
from jsonschema import Draft202012Validator

from xodus.config.settings import EmbeddingSettings, OllamaSettings, is_cloud_model_name
from xodus.providers.base import (
    MODEL_CAPABILITIES,
    CapabilityNotConfirmed,
    Completion,
    InputTooLarge,
    InvalidStructuredOutput,
    ModelInfo,
    ModelNotAvailable,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
    RemoteModelRefused,
    RequestBudget,
    StreamChunk,
    ToolCall,
    ToolSpec,
    Usage,
    estimate_tokens,
)

__all__ = [
    "ModelNotAvailable",
    "OllamaProvider",
    "ProviderError",
    "ProviderTimeout",
    "ProviderUnavailable",
    "StreamChunk",
]

# What Ollama's /api/show "capabilities" imply in XODUS terms. Structured output uses the `format`
# parameter (grammar-constrained sampling), available for any completion model.
_CAPABILITY_MAP = {
    "completion": {"completion", "structured"},
    "tools": {"tools"},
    "thinking": {"thinking"},
    "embedding": {"embedding"},
    "vision": {"vision"},
}


def is_remote_model(entry: dict[str, Any]) -> bool:
    """A model that Ollama runs on another computer. Ollama sets remote_model / remote_host on such models (both are
    omitted when empty in Ollama 0.40.11, so a local alias of a cloud model is caught too) and gives its own cloud
    models a '-cloud' tag."""
    name = str(entry.get("name") or entry.get("model") or "")
    return bool(entry.get("remote_model") or entry.get("remote_host")) or is_cloud_model_name(name)


def find_model(models: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """The /api/tags entry for `name`, or None when it is not installed."""
    return next((m for m in models if name in (m.get("name"), m.get("model"))), None)


def _remoteness(name: str, *descriptions: dict[str, Any] | None) -> tuple[bool, str | None]:
    """Does any of Ollama's descriptions of `name` (its /api/tags entry, its /api/show response) mark it remote?"""
    found = [d for d in descriptions if d]
    remote = is_cloud_model_name(name) or any(is_remote_model(d) for d in found)
    return remote, next((str(d["remote_host"]) for d in found if d.get("remote_host")), None)


def remote_model_error(name: str, host: str | None) -> RemoteModelRefused:
    where = f"Ollama forwards it to {host}" if host else "it is an Ollama cloud or remote model"
    return RemoteModelRefused(f"Model {name!r} does not run on this computer ({where}). XODUS uses local models only; "
                              "configure a model that is installed locally.")


def _error_text(body: str) -> str:
    try:
        return str(json.loads(body).get("error", body))[:500]
    except (ValueError, AttributeError):
        return body[:500]


class EmbeddingDimensionError(ProviderError):
    code = "embedding_dimension_mismatch"


def embed_payload(ollama: OllamaSettings, texts: list[str], dimensions: int | None, num_gpu: int | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"model": ollama.embed_model, "input": texts, "truncate": False,
                               "keep_alive": ollama.keep_alive}
    if dimensions is not None:
        payload["dimensions"] = dimensions
    if num_gpu is not None and num_gpu >= 0:
        payload["options"] = {"num_gpu": num_gpu}  # 0 keeps the embedder on the CPU; a negative value leaves it to Ollama
    return payload


def check_embeddings(body: Any, expected: int, dimensions: int | None) -> list[list[float]]:
    vectors = body.get("embeddings") if isinstance(body, dict) else None
    if not isinstance(vectors, list) or len(vectors) != expected:
        raise ProviderError("Ollama returned an unexpected embedding response.")
    for vector in vectors:
        if not isinstance(vector, list) or (dimensions is not None and len(vector) != dimensions):
            raise EmbeddingDimensionError(
                f"The embedding model returned {len(vector) if isinstance(vector, list) else 'invalid'}-dimensional vectors "
                f"but embedding.dimensions is {dimensions}. Change the configuration or the model.")
    return vectors


def unload_embedder_sync(ollama: OllamaSettings, timeout_s: float = 30.0) -> bool:
    """Ask Ollama to drop the embedding model from memory now (keep_alive 0); the next embedding loads it again. Used by
    the resource governor when it holds ingestion, so the memory ingestion brought in is actually given back instead of
    staying resident for the keep-alive. Ollama loads a model to serve even this request, by default on the GPU, so a model
    that is not resident is left alone. False if Ollama could not be reached or refused."""
    try:
        with httpx.Client(base_url=ollama.base_url, trust_env=False, timeout=timeout_s) as client:
            running = client.get("/api/ps").json().get("models", [])
            if not any((m.get("name") or m.get("model")) == ollama.embed_model for m in running):
                return True
            return client.post("/api/embed", json={"model": ollama.embed_model, "input": [], "keep_alive": 0}).status_code < 400
    except (httpx.HTTPError, ValueError, AttributeError):
        return False


class SyncOllamaEmbedder:
    """Blocking embedding client for the ingestion worker (its own process, no event loop)."""

    def __init__(self, ollama: OllamaSettings, embedding: EmbeddingSettings) -> None:
        self._ollama = ollama
        self._embedding = embedding
        self._client = httpx.Client(base_url=ollama.base_url, trust_env=False,
                                    timeout=httpx.Timeout(embedding.timeout_s, connect=ollama.connect_timeout_s))
        self._checked_local = False

    def close(self) -> None:
        self._client.close()

    def _ensure_local(self) -> None:
        """Refuse an embedding model that Ollama would run on another computer: document text must not leave this one.
        Checked once per worker."""
        if self._checked_local:
            return
        name = self._ollama.embed_model
        try:
            show = self._client.post("/api/show", json={"model": name})
            tags = self._client.get("/api/tags")
        except httpx.TransportError as exc:
            raise ProviderUnavailable(f"Ollama is not reachable at {self._ollama.base_url}.", detail=str(exc)) from exc
        if show.status_code == 404:
            return  # not installed: /api/embed reports that with its own message
        if show.status_code >= 400 or tags.status_code >= 400:
            raise ProviderError(f"Ollama returned HTTP {max(show.status_code, tags.status_code)} while checking "
                                f"the embedding model {name!r}.")
        try:
            remote, host = _remoteness(name, find_model(tags.json().get("models", []), name), show.json())
        except (ValueError, AttributeError) as exc:
            raise ProviderError("Ollama sent an unexpected response while checking the embedding model.") from exc
        if remote:
            raise remote_model_error(name, host)
        self._checked_local = True

    def embed(self, texts: list[str]) -> list[list[float]]:
        self._ensure_local()
        payload = embed_payload(self._ollama, texts, self._embedding.dimensions, self._embedding.num_gpu)
        try:
            resp = self._client.post("/api/embed", json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ProviderUnavailable(f"Ollama is not reachable at {self._ollama.base_url}.", detail=str(exc)) from exc
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"Ollama did not answer within {self._embedding.timeout_s:.0f} s.", detail=str(exc)) from exc
        except (httpx.RemoteProtocolError, httpx.ReadError) as exc:
            raise ProviderUnavailable("The connection to Ollama was lost.", detail=str(exc)) from exc
        if resp.status_code == 404:
            raise ModelNotAvailable(f"Embedding model {self._ollama.embed_model!r} is not installed in Ollama. "
                                    f"Run: ollama pull {self._ollama.embed_model}", detail=_error_text(resp.text))
        if resp.status_code >= 400:
            raise ProviderError(f"Ollama returned HTTP {resp.status_code}: {_error_text(resp.text)}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise ProviderError("Ollama sent a response that is not JSON.") from exc
        return check_embeddings(body, len(texts), self._embedding.dimensions)


def _usage(final: dict[str, Any]) -> Usage:
    ns = 1e-6
    return Usage(
        prompt_tokens=int(final.get("prompt_eval_count") or 0),
        output_tokens=int(final.get("eval_count") or 0),
        total_ms=round((final.get("total_duration") or 0) * ns, 1),
        load_ms=round((final.get("load_duration") or 0) * ns, 1),
    )


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        settings: OllamaSettings,
        transport: httpx.AsyncBaseTransport | None = None,
        on_event: Callable[..., None] | None = None,
        embedding: EmbeddingSettings | None = None,
    ) -> None:
        self._s = settings
        self._embedding = embedding
        self._client = httpx.AsyncClient(
            base_url=settings.base_url,
            transport=transport,
            timeout=httpx.Timeout(settings.request_timeout_s, connect=settings.connect_timeout_s),
            trust_env=False,  # never route loopback traffic through a configured proxy
        )
        self._confirmed: set[str] | None = None
        self._info: ModelInfo | None = None
        self._embed_local: str | None = None  # the embedding model last confirmed to run on this computer
        self._on_event = on_event or (lambda *_args, **_kw: None)

    @property
    def model(self) -> str:
        return self._s.model

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ transport helpers

    def _unavailable(self, exc: Exception) -> ProviderUnavailable:
        return ProviderUnavailable(
            f"Ollama is not reachable at {self._s.base_url}. Start it with scripts\\start-ollama.ps1 "
            "and retry. The XODUS backend is still running.",
            detail=f"{type(exc).__name__}: {exc}",
        )

    def _http_error(self, status: int, body: str) -> ProviderError:
        error = _error_text(body)
        if status == 404:
            return ModelNotAvailable(
                f"Model {self._s.model!r} is not installed in Ollama. Run: ollama pull {self._s.model}", detail=error
            )
        if "does not support" in error:
            return CapabilityNotConfirmed(f"Ollama refused: {error}")
        return ProviderError(f"Ollama returned HTTP {status}: {error}")

    def _timeout(self, timeout_s: float) -> httpx.Timeout:
        return httpx.Timeout(timeout_s, connect=self._s.connect_timeout_s)

    async def _post(self, path: str, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        try:
            resp = await self._client.post(path, json=payload, timeout=self._timeout(timeout_s))
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise self._unavailable(exc) from exc
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"Ollama did not respond within {timeout_s:.0f} s.", detail=str(exc)) from exc
        except (httpx.RemoteProtocolError, httpx.ReadError) as exc:
            raise ProviderUnavailable("The connection to Ollama was lost (was it stopped?).",
                                      detail=f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400:
            raise self._http_error(resp.status_code, resp.text)
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError("Ollama sent a response that is not JSON.", detail=resp.text[:200]) from exc

    # ------------------------------------------------------------------ discovery

    async def health(self) -> dict[str, Any]:
        """Reachability, version, configured model presence/digest and confirmed capabilities."""
        base = {"base_url": self._s.base_url, "model": self._s.model}
        try:
            version = (await self._client.get("/api/version", timeout=self._s.connect_timeout_s)).json()
            tags_resp = await self._client.get("/api/tags", timeout=self._s.connect_timeout_s)
            tags_resp.raise_for_status()
        except httpx.TransportError as exc:
            err = self._unavailable(exc)
            return {**base, "status": "unreachable", "message": err.message, "detail": err.detail}
        except (httpx.HTTPStatusError, ValueError) as exc:
            return {**base, "status": "error", "message": f"Unexpected Ollama response: {exc}"}

        models = tags_resp.json().get("models", [])
        found = find_model(models, self._s.model)
        result = {**base, "ollama_version": version.get("version")}
        for name in (self._s.model, self._s.embed_model):
            entry = find_model(models, name)
            if entry is not None and is_remote_model(entry):
                return {**result, "status": "remote_model",
                        "message": remote_model_error(name, entry.get("remote_host") or None).message}
        if found is None:
            return {**result, "status": "model_missing",
                    "message": f"Model {self._s.model!r} is not installed. Run: ollama pull {self._s.model}"}
        digest = found.get("digest", "")
        result.update(digest=digest, size_bytes=found.get("size"), details=found.get("details", {}),
                      declared_capabilities=sorted(self._s.capabilities),
                      confirmed_capabilities=sorted(self._confirmed) if self._confirmed is not None else None)
        prefix = self._s.expected_digest_prefix
        if prefix and not digest.startswith(prefix):
            return {**result, "status": "digest_mismatch",
                    "message": f"Installed digest {digest[:12]} does not match the pinned {prefix}."}
        return {**result, "status": "ok"}

    async def list_models(self) -> list[dict[str, Any]]:
        try:
            resp = await self._client.get("/api/tags", timeout=self._s.connect_timeout_s)
        except httpx.TransportError as exc:
            raise self._unavailable(exc) from exc
        return resp.json().get("models", [])

    async def model_info(self, model: str | None = None) -> ModelInfo:
        name = model or self._s.model
        try:
            show = await self._post("/api/show", {"model": name}, self._s.connect_timeout_s + 10)
        except ModelNotAvailable as exc:  # name the model that was asked about, not the configured generator
            raise ModelNotAvailable(f"Model {name!r} is not installed in Ollama. Run: ollama pull {name}", detail=exc.detail) from exc
        details = show.get("details", {}) or {}
        info = show.get("model_info", {}) or {}
        context = next((v for k, v in info.items() if k.endswith(".context_length")), None)
        embedding_length = next((v for k, v in info.items() if k.endswith(".embedding_length")), None)
        entry = find_model(await self.list_models(), name)
        remote, remote_host = _remoteness(name, entry, show)
        reported = [str(c) for c in show.get("capabilities", [])]
        mapped = set().union(*(_CAPABILITY_MAP.get(c, set()) for c in reported)) if reported else set()
        declared = set(self._s.capabilities) if name == self._s.model else set()
        return ModelInfo(
            name=name,
            digest=entry.get("digest") if entry else None,
            context_length=int(context) if context else None,
            quantization=details.get("quantization_level"),
            family=details.get("family"),
            parameter_size=details.get("parameter_size"),
            reported_capabilities=reported,
            declared=sorted(declared),
            confirmed=sorted(declared & mapped & MODEL_CAPABILITIES),
            embedding_length=int(embedding_length) if embedding_length else None,
            remote=remote,
            remote_host=remote_host,
        )

    async def probe(self) -> ModelInfo:
        """Confirm the declared capabilities against what Ollama reports for the model."""
        info = await self.model_info()
        self._info = info
        self._confirmed = set(info.confirmed)
        return info

    def confirmed_capabilities(self) -> set[str] | None:
        return None if self._confirmed is None else set(self._confirmed)

    async def _local_info(self) -> ModelInfo:
        """The active model's probe result (probed again when the model changed); refuses a model that runs elsewhere."""
        if self._info is None or self._info.name != self._s.model:
            await self.probe()
        assert self._info is not None
        if self._info.remote:
            raise remote_model_error(self._info.name, self._info.remote_host)
        return self._info

    async def _require(self, capability: str) -> None:
        await self._local_info()
        assert self._confirmed is not None
        if capability not in self._confirmed:
            declared = sorted(self._s.capabilities)
            reported = self._info.reported_capabilities if self._info else []
            raise CapabilityNotConfirmed(
                f"The model {self._s.model!r} does not have the {capability!r} capability confirmed "
                f"(declared in config: {declared}; reported by Ollama: {reported})."
            )

    # ------------------------------------------------------------------ requests

    def _options(self, budget: RequestBudget, seed: int | None) -> dict[str, Any]:
        options: dict[str, Any] = {
            "num_ctx": budget.num_ctx,  # always explicit (resource rule: set num_ctx on every request)
            "num_predict": budget.max_output_tokens,
            **self._s.sampling.model_dump(),
        }
        if seed is not None:
            options["seed"] = seed
        return options

    def _check_input(self, messages: list[dict[str, Any]], budget: RequestBudget, tools: list[ToolSpec] | None = None) -> None:
        estimate = estimate_tokens(messages, tools)
        if estimate > budget.input_limit():
            raise InputTooLarge(
                f"The request is about {estimate} tokens; the limit is {budget.input_limit()} "
                f"(num_ctx {budget.num_ctx} minus {budget.max_output_tokens} reserved for the answer)."
            )

    async def _think(self, budget: RequestBudget) -> dict[str, Any]:
        if budget.thinking:
            await self._require("thinking")
            return {"think": True}
        # Thinking models think by default and can spend the whole output budget on it, answering nothing; so whenever
        # Ollama reports the capability, switch it off explicitly, declared or not (only turning it ON needs a declaration).
        if (self._confirmed is not None and "thinking" in self._confirmed) or (
                self._info is not None and "thinking" in self._info.reported_capabilities):
            return {"think": False}
        return {}

    async def _payload(self, messages: list[dict[str, Any]], budget: RequestBudget, seed: int | None, *,
                       stream: bool, tools: list[ToolSpec] | None = None) -> dict[str, Any]:
        await self._require("completion")
        self._check_input(messages, budget, tools)
        payload: dict[str, Any] = {
            "model": self._s.model,
            "messages": messages,
            "stream": stream,
            "keep_alive": self._s.keep_alive,
            "options": self._options(budget, seed),
            **await self._think(budget),
        }
        return payload

    def _completion(self, body: dict[str, Any]) -> Completion:
        message = body.get("message", {}) or {}
        calls = []
        for call in message.get("tool_calls") or []:
            fn = call.get("function", {}) if isinstance(call, dict) else {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    pass  # left as a string: the StepGate rejects non-object arguments
            calls.append(ToolCall(name=str(fn.get("name", "")), arguments=args))
        return Completion(
            content=str(message.get("content") or ""),
            tool_calls=calls,
            usage=_usage(body),
            thinking=message.get("thinking"),
            done_reason=body.get("done_reason"),
        )

    async def generate(self, messages: list[dict[str, Any]], budget: RequestBudget, *, seed: int | None = None) -> Completion:
        payload = await self._payload(messages, budget, seed, stream=False)
        return self._completion(await self._post("/api/chat", payload, budget.timeout_s))

    async def structured(self, messages: list[dict[str, Any]], schema: dict[str, Any], budget: RequestBudget, *,
                         seed: int | None = None) -> tuple[Any, Completion]:
        await self._require("structured")
        payload = await self._payload(messages, budget, seed, stream=False)
        payload["format"] = schema
        completion = self._completion(await self._post("/api/chat", payload, budget.timeout_s))
        try:
            data = json.loads(completion.content)
        except ValueError as exc:
            raise InvalidStructuredOutput("The model's structured output is not valid JSON.",
                                          detail=completion.content[:300]) from exc
        error = next(iter(Draft202012Validator(schema).iter_errors(data)), None)
        if error is not None:
            raise InvalidStructuredOutput(f"The model's structured output does not match the schema: {error.message[:300]}",
                                          detail=completion.content[:300])
        return data, completion

    async def chat_with_tools(self, messages: list[dict[str, Any]], tools: list[ToolSpec], budget: RequestBudget, *,
                              seed: int | None = None) -> Completion:
        await self._require("tools")
        payload = await self._payload(messages, budget, seed, stream=False, tools=tools)
        payload["tools"] = [
            {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in tools
        ]
        return self._completion(await self._post("/api/chat", payload, budget.timeout_s))

    async def stream(self, messages: list[dict[str, Any]], budget: RequestBudget, *,
                     seed: int | None = None) -> AsyncIterator[StreamChunk]:
        payload = await self._payload(messages, budget, seed, stream=True)
        try:
            async with self._client.stream("POST", "/api/chat", json=payload, timeout=self._timeout(budget.timeout_s)) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    raise self._http_error(resp.status_code, body)
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError as exc:
                        raise ProviderError("Ollama sent a malformed stream line.", detail=line[:200]) from exc
                    if "error" in obj:
                        raise ProviderError(f"Ollama error: {obj['error']}")
                    if obj.get("done"):
                        yield StreamChunk(final=obj, usage=_usage(obj))
                        return
                    text = obj.get("message", {}).get("content", "")
                    if text:
                        yield StreamChunk(text=text)
                raise ProviderError("Ollama closed the stream before it finished.")
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise self._unavailable(exc) from exc
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"Ollama did not respond within {budget.timeout_s:.0f} s.", detail=str(exc)) from exc
        except (httpx.RemoteProtocolError, httpx.ReadError) as exc:
            raise ProviderUnavailable(
                "The connection to Ollama was lost mid-response (was it stopped?). The XODUS backend is still running.",
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc

    async def stream_chat(self, messages: list[dict[str, Any]], *, num_ctx: int, max_output_tokens: int,
                          seed: int | None = None) -> AsyncIterator[StreamChunk]:
        """Gate B entry point, kept for compatibility: stream() with a budget built from the arguments."""
        budget = RequestBudget(num_ctx=num_ctx, max_output_tokens=max_output_tokens, timeout_s=self._s.request_timeout_s)
        async for chunk in self.stream(messages, budget, seed=seed):
            yield chunk

    async def _check_embed_model(self) -> None:
        name = self._s.embed_model
        if self._embed_local == name:
            return
        try:
            info = await self.model_info(name)
        except ModelNotAvailable:
            return  # not installed: /api/embed reports that with its own message
        if info.remote:
            raise remote_model_error(name, info.remote_host)
        self._embed_local = name

    async def embed(self, texts: list[str], *, timeout_s: float | None = None, dimensions: int | None = None) -> list[list[float]]:
        """Embed texts with ollama.embed_model, placed as configured (embedding.num_gpu)."""
        await self._check_embed_model()
        emb = self._embedding
        dims = dimensions if dimensions is not None else (emb.dimensions if emb else None)
        timeout = timeout_s if timeout_s is not None else (emb.timeout_s if emb else 60.0)
        payload = embed_payload(self._s, texts, dims, emb.num_gpu if emb else None)
        try:
            body = await self._post("/api/embed", payload, timeout)
        except ModelNotAvailable as exc:
            raise ModelNotAvailable(f"Embedding model {self._s.embed_model!r} is not installed in Ollama. "
                                    f"Run: ollama pull {self._s.embed_model}", detail=exc.detail) from exc
        return check_embeddings(body, len(texts), dims)

    async def load(self, *, num_ctx: int) -> None:
        await self._local_info()
        await self._post("/api/generate", {"model": self._s.model, "keep_alive": self._s.keep_alive,
                                           "options": {"num_ctx": num_ctx}}, self._s.request_timeout_s)
        self._on_event("model_loaded", model=self._s.model, num_ctx=num_ctx)

    async def unload(self) -> None:
        if self._info is not None and self._info.name == self._s.model and self._info.remote:
            return  # nothing of a remote model is loaded on this computer; a switch away from it goes ahead
        await self._post("/api/generate", {"model": self._s.model, "keep_alive": 0}, self._s.request_timeout_s)
        self._on_event("model_unloaded", model=self._s.model)

    async def running_models(self) -> list[dict[str, Any]]:
        """Ollama's own view of loaded models (/api/ps); a cross-check only."""
        try:
            resp = await self._client.get("/api/ps", timeout=self._s.connect_timeout_s)
            resp.raise_for_status()
            return resp.json().get("models", [])
        except (httpx.HTTPError, ValueError):
            return []
