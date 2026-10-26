"""Ollama adapter: the only module that knows Ollama's HTTP API.

Gate B scope: health/model check and streamed chat. The full ModelProvider protocol
(structured output, tools, embeddings, load/unload) arrives in Gate C.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from xodus.config.settings import OllamaSettings


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


@dataclass(frozen=True)
class StreamChunk:
    text: str = ""
    final: dict[str, Any] | None = None


class OllamaProvider:
    def __init__(self, settings: OllamaSettings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._s = settings
        self._client = httpx.AsyncClient(
            base_url=settings.base_url,
            transport=transport,
            timeout=httpx.Timeout(settings.request_timeout_s, connect=settings.connect_timeout_s),
            trust_env=False,  # never route loopback traffic through a configured proxy
        )

    @property
    def model(self) -> str:
        return self._s.model

    async def aclose(self) -> None:
        await self._client.aclose()

    def _unavailable(self, exc: Exception) -> ProviderUnavailable:
        return ProviderUnavailable(
            f"Ollama is not reachable at {self._s.base_url}. Start it with scripts\\start-ollama.ps1 "
            "and retry. The XODUS backend is still running.",
            detail=f"{type(exc).__name__}: {exc}",
        )

    async def health(self) -> dict[str, Any]:
        """Report reachability, version and whether the configured model (and digest) is present."""
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

        found = next(
            (m for m in tags_resp.json().get("models", []) if self._s.model in (m.get("name"), m.get("model"))),
            None,
        )
        result = {**base, "ollama_version": version.get("version")}
        if found is None:
            return {
                **result,
                "status": "model_missing",
                "message": f"Model {self._s.model!r} is not installed. Run: ollama pull {self._s.model}",
            }
        digest = found.get("digest", "")
        result.update(digest=digest, size_bytes=found.get("size"), details=found.get("details", {}))
        prefix = self._s.expected_digest_prefix
        if prefix and not digest.startswith(prefix):
            return {
                **result,
                "status": "digest_mismatch",
                "message": f"Installed digest {digest[:12]} does not match the pinned {prefix}.",
            }
        return {**result, "status": "ok"}

    async def running_models(self) -> list[dict[str, Any]]:
        """Ollama's own view of loaded models (/api/ps); used only as a cross-check."""
        try:
            resp = await self._client.get("/api/ps", timeout=self._s.connect_timeout_s)
            resp.raise_for_status()
            return resp.json().get("models", [])
        except (httpx.HTTPError, ValueError):
            return []

    async def stream_chat(
        self,
        messages: list[dict[str, str]],
        *,
        num_ctx: int,
        max_output_tokens: int,
        seed: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        options: dict[str, Any] = {
            "num_ctx": num_ctx,  # always explicit (resource rule: set num_ctx on every request)
            "num_predict": max_output_tokens,
            **self._s.sampling.model_dump(),
        }
        if seed is not None:
            options["seed"] = seed
        payload = {
            "model": self._s.model,
            "messages": messages,
            "stream": True,
            "keep_alive": self._s.keep_alive,
            "options": options,
        }
        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    error = _error_text(body)
                    if resp.status_code == 404:
                        raise ModelNotAvailable(
                            f"Model {self._s.model!r} is not installed in Ollama. Run: ollama pull {self._s.model}",
                            detail=error,
                        )
                    raise ProviderError(f"Ollama returned HTTP {resp.status_code}: {error}")
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
                        yield StreamChunk(final=obj)
                        return
                    text = obj.get("message", {}).get("content", "")
                    if text:
                        yield StreamChunk(text=text)
                raise ProviderError("Ollama closed the stream before it finished.")
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise self._unavailable(exc) from exc
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(
                f"Ollama did not respond within {self._s.request_timeout_s:.0f} s.", detail=str(exc)
            ) from exc
        except (httpx.RemoteProtocolError, httpx.ReadError) as exc:
            raise ProviderUnavailable(
                "The connection to Ollama was lost mid-response (was it stopped?). "
                "The XODUS backend is still running.",
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc


def _error_text(body: str) -> str:
    try:
        return str(json.loads(body).get("error", body))[:500]
    except ValueError:
        return body[:500]
