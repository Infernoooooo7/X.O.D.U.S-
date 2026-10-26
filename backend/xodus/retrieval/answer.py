"""Answering a question from the user's documents, with checked citations.

The model is given the question and the retrieved passages and nothing else: no tools, no way to start a task.
Passages are untrusted data (they may contain instructions); the code, not the prompt, decides what a passage
can cause, and it can cause nothing but words in the answer. When retrieval finds no sufficient evidence the model
is not called at all.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from xodus.config.settings import Settings
from xodus.providers.base import ModelProvider, ProviderError, RequestBudget, estimate_tokens
from xodus.resources.admission import AdmissionGate, QueueFull
from xodus.retrieval.citations import INSUFFICIENT_PHRASE, validate_answer
from xodus.retrieval.hybrid import Evidence, QueryError, Retriever
from xodus.security.untrusted import UNTRUSTED_NOTICE, wrap_untrusted

PROMPT_VERSION = "kb-answer-1"
_SAFE_SOURCE = re.compile(r"[^A-Za-z0-9 ._:/\\()+,-]")

SYSTEM = (
    "You answer the user's question using only the numbered evidence passages below, which come from the user's own "
    "documents. After each claim, cite the passage it came from as [chunk:ID] using the ID shown with the passage. "
    "Cite only IDs that appear below. If the evidence does not contain the answer, reply exactly: "
    f"{INSUFFICIENT_PHRASE} Do not use outside knowledge. " + UNTRUSTED_NOTICE
)


def safe_source(label: str) -> str:
    return _SAFE_SOURCE.sub("_", label)[:120]


def build_messages(question: str, evidence: list[Evidence]) -> list[dict[str, Any]]:
    blocks = []
    for number, item in enumerate(evidence, start=1):
        place = ", ".join(p for p in (item.document, item.locator or "", item.heading_path.replace(">", "/")) if p)
        label = safe_source(f"chunk:{item.chunk_id} {place}")
        blocks.append(f"Evidence [chunk:{item.chunk_id}]:\n" + wrap_untrusted(item.text, source=label, step=number))
    user = "Evidence:\n\n" + "\n\n".join(blocks) + f"\n\nQuestion: {question}"
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


class AnswerService:
    def __init__(self, retriever: Retriever, provider: ModelProvider, admission: AdmissionGate, settings: Settings) -> None:
        self.retriever = retriever
        self.provider = provider
        self.admission = admission
        self.settings = settings

    async def stream(self, question: str, collection_ids: list[int], k: int | None = None) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Events: meta, token*, done | error. `done` carries the validated answer and its citation report."""
        request_id = uuid.uuid4().hex[:12]
        t0 = time.perf_counter()
        s = self.settings
        budget = RequestBudget(num_ctx=s.ollama.num_ctx, max_output_tokens=s.ollama.max_output_tokens,
                               timeout_s=s.ollama.request_timeout_s)
        overhead = estimate_tokens([{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}]) + 200
        evidence_budget = max(200, min(s.knowledge.evidence_token_budget, budget.input_limit() - overhead))
        try:
            result = await self.retriever.search(question, collection_ids, k=k, origin="ask", request_id=request_id,
                                                 token_budget=evidence_budget)
        except QueryError as exc:
            yield "error", {"request_id": request_id, "code": "invalid_query", "message": str(exc)}
            return
        context_ids = [e.chunk_id for e in result.evidence]
        yield "meta", {"request_id": request_id, "model": self.provider.model, "evidence": [e.summary() for e in result.evidence],
                       "sufficient": result.sufficient, "mode": result.mode, "degraded": result.degraded,
                       "reasons": result.reasons, "stats": result.stats}
        if not result.sufficient:
            yield "done", {"request_id": request_id, "answer": INSUFFICIENT_PHRASE, "sufficient": False, "model_called": False,
                           "citations": {"valid": [], "invalid": []}, "uncited": False, "insufficient": True,
                           "timing": {"total_ms": round((time.perf_counter() - t0) * 1000, 1)}}
            return

        messages = build_messages(question, result.evidence)
        parts: list[str] = []
        usage = None
        t_first: float | None = None
        try:
            async with self.admission.slot():
                async for chunk in self.provider.stream(messages, budget, seed=s.executor.seed):
                    if chunk.usage is not None:
                        usage = chunk.usage
                    elif chunk.text:
                        if t_first is None:
                            t_first = time.perf_counter()
                        parts.append(chunk.text)
                        yield "token", {"t": chunk.text}
        except QueueFull as exc:
            yield "error", {"request_id": request_id, "code": "queue_full", "message": str(exc)}
            return
        except ProviderError as exc:
            yield "error", {"request_id": request_id, "code": exc.code, "message": exc.message, "partial": bool(parts)}
            return
        except asyncio.CancelledError:
            raise
        answer = "".join(parts).strip()
        report = validate_answer(answer, context_ids)
        yield "done", {
            "request_id": request_id, "answer": report.cleaned, "sufficient": True, "model_called": True,
            "citations": {"valid": report.valid, "invalid": report.invalid}, "uncited": report.uncited,
            "insufficient": report.insufficient, "prompt_version": PROMPT_VERSION,
            "usage": None if usage is None else {"prompt_tokens": usage.prompt_tokens, "output_tokens": usage.output_tokens},
            "timing": {"ttft_ms": None if t_first is None else round((t_first - t0) * 1000, 1),
                       "total_ms": round((time.perf_counter() - t0) * 1000, 1)},
        }
