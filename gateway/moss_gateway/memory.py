from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from threading import RLock
from typing import Any, Awaitable, Callable
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

from .events import EventBus, sanitize_payload
from .tools import ToolRegistry


_ALLOWED_CATEGORIES = {"profile", "preference", "fact", "routine", "device"}
_SENSITIVE_KEY = re.compile(r"(password|passwd|secret|token|api[_-]?key|credential)", re.IGNORECASE)
_SENSITIVE_VALUE = re.compile(
    r"(-----BEGIN [A-Z ]*PRIVATE KEY-----|\bBearer\s+[A-Za-z0-9._~+/=-]{12,})",
    re.IGNORECASE,
)


class MemoryError(RuntimeError):
    pass


class MemoryWriteRequest(BaseModel):
    key: str = Field(min_length=1, max_length=80)
    category: str = Field(min_length=1, max_length=32)
    value: str = Field(min_length=1, max_length=2000)
    source: str = Field(default="explicit", min_length=1, max_length=120)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("category")
    @classmethod
    def validate_category(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in _ALLOWED_CATEGORIES:
            raise ValueError(f"unsupported memory category: {normalized}")
        return normalized

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        normalized = value.strip()
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:")
        if not normalized or any(char not in allowed for char in normalized):
            raise ValueError("memory key contains unsupported characters")
        if _SENSITIVE_KEY.search(normalized):
            raise ValueError("memory key appears to describe a credential or secret")
        return normalized

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("memory value must not be empty")
        if _SENSITIVE_VALUE.search(normalized):
            raise ValueError("memory value appears to contain a credential or private key")
        return normalized


@dataclass(frozen=True, slots=True)
class MemoryConfig:
    db_path: str
    max_entries: int = 5000


class HostMemoryStore:
    def __init__(self, config: MemoryConfig, events: EventBus) -> None:
        self.config = config
        self.events = events
        self._lock = RLock()
        self._initialized = False

    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            path = Path(self.config.db_path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._db_path = str(path)
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS memories (
                        id TEXT PRIMARY KEY,
                        key TEXT NOT NULL UNIQUE,
                        category TEXT NOT NULL,
                        value TEXT NOT NULL,
                        source TEXT NOT NULL,
                        confidence REAL NOT NULL,
                        revision INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_memories_category
                    ON memories(category, updated_at DESC);
                    """
                )
            self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def status(self) -> dict[str, Any]:
        self.initialize()
        with self._lock, self._connect() as connection:
            total = int(connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
            rows = connection.execute(
                "SELECT category, COUNT(*) AS count FROM memories GROUP BY category"
            ).fetchall()
        return {
            "scope": "host-long-term",
            "storage": "sqlite",
            "automatic_learning": False,
            "entries": total,
            "max_entries": self.config.max_entries,
            "categories": {str(row["category"]): int(row["count"]) for row in rows},
        }

    def remember(self, request: MemoryWriteRequest) -> dict[str, Any]:
        self.initialize()
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM memories WHERE key = ?", (request.key,)
            ).fetchone()
            if existing is None:
                total = int(connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
                if total >= self.config.max_entries:
                    raise MemoryError("host memory entry limit reached")
                memory_id = f"mem_{uuid4().hex[:16]}"
                revision = 1
                connection.execute(
                    """
                    INSERT INTO memories (
                        id, key, category, value, source, confidence,
                        revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        request.key,
                        request.category,
                        request.value,
                        request.source,
                        request.confidence,
                        revision,
                        now,
                        now,
                    ),
                )
            else:
                memory_id = str(existing["id"])
                revision = int(existing["revision"]) + 1
                connection.execute(
                    """
                    UPDATE memories
                    SET category = ?, value = ?, source = ?, confidence = ?,
                        revision = ?, updated_at = ?
                    WHERE key = ?
                    """,
                    (
                        request.category,
                        request.value,
                        request.source,
                        request.confidence,
                        revision,
                        now,
                        request.key,
                    ),
                )
        result = self.get(request.key)
        self.events.publish(
            "gateway",
            "memory_updated",
            {
                "memory_id": memory_id,
                "key": request.key,
                "category": request.category,
                "revision": revision,
                "source": request.source,
            },
        )
        return result

    def get(self, key: str) -> dict[str, Any]:
        self.initialize()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM memories WHERE key = ?", (key,)
            ).fetchone()
        item = self._row(row)
        if item is None:
            raise KeyError(key)
        return item

    def list(
        self,
        *,
        category: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        self.initialize()
        bounded_limit = max(1, min(limit, 200))
        bounded_offset = max(0, min(offset, 100000))
        if category is not None:
            category = category.strip().lower()
            if category not in _ALLOWED_CATEGORIES:
                raise ValueError(f"unsupported memory category: {category}")
        with self._lock, self._connect() as connection:
            if category:
                rows = connection.execute(
                    """
                    SELECT * FROM memories WHERE category = ?
                    ORDER BY updated_at DESC LIMIT ? OFFSET ?
                    """,
                    (category, bounded_limit, bounded_offset),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM memories ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (bounded_limit, bounded_offset),
                ).fetchall()
        return [dict(row) for row in rows]

    def search(
        self,
        query: str,
        *,
        category: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        self.initialize()
        query = query.strip()
        if not query or len(query) > 200:
            raise ValueError("memory search query must be 1-200 characters")
        candidates = self.list(category=category, limit=min(self.config.max_entries, 5000), offset=0)
        terms = [term for term in re.split(r"\s+", query.lower()) if term]
        scored: list[tuple[int, dict[str, Any]]] = []
        for item in candidates:
            key = str(item["key"]).lower()
            value = str(item["value"]).lower()
            source = str(item["source"]).lower()
            score = 0
            for term in terms:
                if term in key:
                    score += 4
                if term in value:
                    score += 2
                if term in source:
                    score += 1
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda pair: (pair[0], pair[1]["updated_at"]), reverse=True)
        return [item for _, item in scored[: max(1, min(limit, 50))]]

    def forget(self, key: str) -> dict[str, Any]:
        self.initialize()
        current = self.get(key)
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM memories WHERE key = ?", (key,))
        self.events.publish(
            "gateway",
            "memory_deleted",
            {
                "memory_id": current["id"],
                "key": current["key"],
                "category": current["category"],
            },
        )
        return {"deleted": True, "key": key}

    def planner_context(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        return [
            {
                "key": item["key"],
                "category": item["category"],
                "value": item["value"],
                "confidence": item["confidence"],
            }
            for item in self.search(query, limit=limit)
        ]


def register_memory_tools(registry: ToolRegistry, memory: HostMemoryStore) -> None:
    registry.register(
        name="memory.status",
        description="Read Host/RDK long-term memory status. Automatic learning is disabled.",
        risk="read_only",
        handler=lambda _: memory.status(),
    )
    registry.register(
        name="memory.list",
        description="List explicitly stored Host/RDK memories.",
        risk="read_only",
        input_schema={
            "type": "object",
            "properties": {
                "category": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "offset": {"type": "integer", "minimum": 0},
            },
            "additionalProperties": False,
        },
        handler=lambda args: memory.list(
            category=args.get("category"),
            limit=int(args.get("limit", 100)),
            offset=int(args.get("offset", 0)),
        ),
    )
    registry.register(
        name="memory.search",
        description="Search explicitly stored Host/RDK memories by text.",
        risk="read_only",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "category": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=lambda args: memory.search(
            str(args["query"]),
            category=args.get("category"),
            limit=int(args.get("limit", 10)),
        ),
    )
    registry.register(
        name="memory.get",
        description="Read one explicit Host/RDK memory by key.",
        risk="read_only",
        input_schema={
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
        handler=lambda args: memory.get(str(args["key"])),
    )
    registry.register(
        name="memory.remember",
        description="Explicitly create or update a Host/RDK long-term memory. Never use for credentials or secrets.",
        risk="sensitive",
        input_schema={
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "category": {"type": "string"},
                "value": {"type": "string"},
                "source": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["key", "category", "value"],
            "additionalProperties": False,
        },
        handler=lambda args: memory.remember(MemoryWriteRequest.model_validate(args)),
    )
    registry.register(
        name="memory.forget",
        description="Explicitly delete one Host/RDK long-term memory by key.",
        risk="sensitive",
        input_schema={
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
        handler=lambda args: memory.forget(str(args["key"])),
    )


def install_memory_routes(
    app: FastAPI,
    memory: HostMemoryStore,
    require_admin: Callable[..., Awaitable[None]],
) -> None:
    dependencies = [Depends(require_admin)]

    @app.get("/api/v1/memory/status", dependencies=dependencies)
    async def memory_status() -> dict[str, Any]:
        return memory.status()

    @app.get("/api/v1/memory", dependencies=dependencies)
    async def list_memory(category: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        try:
            items = memory.list(category=category, limit=limit, offset=offset)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)[:500]) from None
        return {"memories": items, "count": len(items)}

    @app.get("/api/v1/memory/search", dependencies=dependencies)
    async def search_memory(query: str, category: str | None = None, limit: int = 10) -> dict[str, Any]:
        try:
            items = memory.search(query, category=category, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)[:500]) from None
        return {"memories": items, "count": len(items)}

    @app.get("/api/v1/memory/{key}", dependencies=dependencies)
    async def get_memory(key: str) -> dict[str, Any]:
        try:
            return memory.get(key)
        except KeyError:
            raise HTTPException(status_code=404, detail="memory not found") from None

    @app.post("/api/v1/memory", dependencies=dependencies)
    async def remember(request: MemoryWriteRequest) -> dict[str, Any]:
        try:
            return memory.remember(request)
        except MemoryError as exc:
            raise HTTPException(status_code=409, detail=str(exc)[:500]) from None

    @app.delete("/api/v1/memory/{key}", dependencies=dependencies)
    async def forget(key: str) -> dict[str, Any]:
        try:
            return memory.forget(key)
        except KeyError:
            raise HTTPException(status_code=404, detail="memory not found") from None
