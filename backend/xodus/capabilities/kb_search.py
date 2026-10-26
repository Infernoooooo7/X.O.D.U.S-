"""kb.search: a read-only capability that searches knowledge collections the configuration grants (kb.read:<name>).

A model-invoked search goes through the StepGate like every capability: the collection argument is an enum of the
granted collections, the permissions are declared per collection, and the result is returned to the model as
untrusted data (the executor wraps it and marks the task as having read untrusted content).
"""

from __future__ import annotations

from typing import Any

from xodus.capabilities.manifest import CapabilityManifest
from xodus.capabilities.registry import InProcessCapability, InvalidInput, InvocationContext, NotFound, PermissionDenied
from xodus.retrieval.hybrid import QueryError, Retriever
from xodus.storage.knowledge import KnowledgeStore

MAX_TEXT_CHARS = 1500

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["results", "sufficient", "mode"],
    "properties": {
        "sufficient": {"type": "boolean"},
        "mode": {"type": "string"},
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["chunk_id", "document", "text"],
                "properties": {
                    "chunk_id": {"type": "integer"}, "document": {"type": "string"}, "heading": {"type": "string"},
                    "locator": {"type": ["string", "null"]}, "score": {"type": "number"}, "text": {"type": "string"},
                },
            },
        },
    },
}


class KnowledgeSearch(InProcessCapability):
    def __init__(self, retriever: Retriever, store: KnowledgeStore, readable: list[str]) -> None:
        if not readable:
            raise ValueError("kb.search needs at least one readable collection")
        self.retriever = retriever
        self.store = store
        self.readable = sorted(readable)
        self.manifest = CapabilityManifest(
            id="kb.search",
            name="Search documents",
            description="Search the user's ingested documents in one collection and return the best matching passages "
                        "with their chunk ids. Read-only.",
            version="0.1.0",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["query", "collection"],
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 500, "description": "What to look for."},
                    "collection": {"enum": self.readable, "description": "Collection to search."},
                    "k": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                },
            },
            output_schema=OUTPUT_SCHEMA,
            permissions=(*(f"kb.read:{name}" for name in self.readable), "net.none", "exec.none"),
            execution_mode="in_process",
            process_tier="none",
            resource_class="model",
            timeout_s=60.0,
            max_output_bytes=64_000,
            cancellable=False,
            requires_approval="never",
            side_effects="none",
        )

    async def invoke(self, args: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
        name = args.get("collection")
        if name not in self.readable:  # defence in depth: the schema enum already restricts this
            raise PermissionDenied(f"Collection {name!r} is not readable by capabilities.")
        coll = self.store.collection_by_name(name)
        if coll is None:
            raise NotFound(f"No collection named {name!r} exists yet.")
        try:
            result = await self.retriever.search(args["query"], [coll["id"]], k=int(args.get("k", 5)), origin="capability")
        except QueryError as exc:
            raise InvalidInput(str(exc)) from exc
        return {
            "sufficient": result.sufficient,
            "mode": result.mode,
            "results": [{"chunk_id": e.chunk_id, "document": e.document, "heading": e.heading_path, "locator": e.locator,
                         "score": round(e.score, 5), "text": e.text[:MAX_TEXT_CHARS]} for e in result.evidence],
        }
