from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any, Awaitable, Callable
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from .events import EventBus, sanitize_payload
from .tools import ToolRegistry


class MissionError(RuntimeError):
    pass


class MissionPolicyError(MissionError):
    pass


class MissionStep(BaseModel):
    tool: str = Field(min_length=1, max_length=120)
    arguments: dict[str, Any] = Field(default_factory=dict)


class MissionCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    steps: list[MissionStep] = Field(min_length=1, max_length=20)
    run_at: datetime | None = None
    interval_seconds: int | None = Field(default=None, ge=60, le=604800)
    enabled: bool = True
    max_retries: int = Field(default=0, ge=0, le=3)
    retry_delay_seconds: int = Field(default=30, ge=10, le=3600)

    @field_validator("run_at")
    @classmethod
    def normalize_run_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("run_at must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_schedule(self) -> "MissionCreateRequest":
        if self.interval_seconds is not None and self.run_at is None:
            self.run_at = datetime.now(timezone.utc) + timedelta(seconds=self.interval_seconds)
        return self


@dataclass(frozen=True, slots=True)
class MissionConfig:
    db_path: str
    tick_seconds: int = 2
    heartbeat_seconds: int = 30
    max_concurrent: int = 1
    allowed_risks: tuple[str, ...] = ("read_only", "low_impact")


class MissionStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = RLock()
        self._initialized = False

    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            if self.db_path != ":memory:":
                path = Path(self.db_path).expanduser()
                path.parent.mkdir(parents=True, exist_ok=True)
                self.db_path = str(path)
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS missions (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        steps_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        enabled INTEGER NOT NULL,
                        run_at TEXT,
                        interval_seconds INTEGER,
                        next_run_at TEXT,
                        cancel_requested INTEGER NOT NULL DEFAULT 0,
                        max_retries INTEGER NOT NULL DEFAULT 0,
                        retry_delay_seconds INTEGER NOT NULL DEFAULT 30,
                        consecutive_failures INTEGER NOT NULL DEFAULT 0,
                        run_count INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        last_started_at TEXT,
                        last_finished_at TEXT,
                        last_error TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_missions_due
                    ON missions(enabled, next_run_at);
                    CREATE TABLE IF NOT EXISTS mission_runs (
                        id TEXT PRIMARY KEY,
                        mission_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        result_json TEXT,
                        error TEXT,
                        FOREIGN KEY(mission_id) REFERENCES missions(id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_mission_runs_mission
                    ON mission_runs(mission_id, started_at DESC);
                    """
                )
            self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        item["cancel_requested"] = bool(item["cancel_requested"])
        item["steps"] = json.loads(item.pop("steps_json"))
        return item

    def create(self, request: MissionCreateRequest) -> dict[str, Any]:
        self.initialize()
        now = datetime.now(timezone.utc)
        mission_id = f"mission_{uuid4().hex[:16]}"
        run_at = request.run_at.isoformat() if request.run_at else None
        next_run_at = run_at if request.enabled else None
        status = "scheduled" if next_run_at else "idle"
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO missions (
                    id, title, steps_json, status, enabled, run_at,
                    interval_seconds, next_run_at, max_retries,
                    retry_delay_seconds, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mission_id,
                    request.title,
                    json.dumps([step.model_dump() for step in request.steps], ensure_ascii=False),
                    status,
                    1 if request.enabled else 0,
                    run_at,
                    request.interval_seconds,
                    next_run_at,
                    request.max_retries,
                    request.retry_delay_seconds,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
        return self.get(mission_id)

    def get(self, mission_id: str) -> dict[str, Any]:
        self.initialize()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM missions WHERE id = ?", (mission_id,)
            ).fetchone()
        item = self._row(row)
        if item is None:
            raise KeyError(mission_id)
        return item

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        self.initialize()
        bounded = max(1, min(limit, 200))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM missions ORDER BY created_at DESC LIMIT ?", (bounded,)
            ).fetchall()
        return [self._row(row) for row in rows if row is not None]  # type: ignore[list-item]

    def list_due(self, now_iso: str, limit: int = 20) -> list[str]:
        self.initialize()
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM missions
                WHERE enabled = 1 AND next_run_at IS NOT NULL
                  AND next_run_at <= ? AND status != 'running'
                ORDER BY next_run_at ASC LIMIT ?
                """,
                (now_iso, max(1, min(limit, 100))),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def update(self, mission_id: str, **fields: Any) -> dict[str, Any]:
        self.initialize()
        allowed = {
            "status", "enabled", "next_run_at", "cancel_requested",
            "consecutive_failures", "run_count", "updated_at",
            "last_started_at", "last_finished_at", "last_error",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unsupported mission fields: {sorted(unknown)}")
        if not fields:
            return self.get(mission_id)
        normalized = {
            key: (1 if value else 0) if key in {"enabled", "cancel_requested"} else value
            for key, value in fields.items()
        }
        assignments = ", ".join(f"{key} = ?" for key in normalized)
        values = list(normalized.values()) + [mission_id]
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE missions SET {assignments} WHERE id = ?", values
            )
            if cursor.rowcount == 0:
                raise KeyError(mission_id)
        return self.get(mission_id)

    def create_run(self, mission_id: str) -> str:
        self.initialize()
        run_id = f"run_{uuid4().hex[:16]}"
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO mission_runs (id, mission_id, status, started_at) VALUES (?, ?, 'running', ?)",
                (run_id, mission_id, datetime.now(timezone.utc).isoformat()),
            )
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        self.initialize()
        result_json = None
        if result is not None:
            raw = json.dumps(sanitize_payload(result), ensure_ascii=False, separators=(",", ":"))
            result_json = raw[:32768]
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE mission_runs SET status = ?, finished_at = ?, result_json = ?, error = ?
                WHERE id = ?
                """,
                (
                    status,
                    datetime.now(timezone.utc).isoformat(),
                    result_json,
                    error[:1000] if error else None,
                    run_id,
                ),
            )

    def list_runs(self, mission_id: str, limit: int = 20) -> list[dict[str, Any]]:
        self.initialize()
        self.get(mission_id)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM mission_runs WHERE mission_id = ?
                ORDER BY started_at DESC LIMIT ?
                """,
                (mission_id, max(1, min(limit, 100))),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            if item.get("result_json"):
                try:
                    item["result"] = json.loads(item.pop("result_json"))
                except json.JSONDecodeError:
                    item["result"] = "<invalid-stored-result>"
                    item.pop("result_json", None)
            else:
                item.pop("result_json", None)
            result.append(item)
        return result

    def counts(self) -> dict[str, int]:
        self.initialize()
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM missions GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}


class MissionEngine:
    def __init__(
        self,
        config: MissionConfig,
        tools: ToolRegistry,
        events: EventBus,
    ) -> None:
        self.config = config
        self.tools = tools
        self.events = events
        self.store = MissionStore(config.db_path)
        self._scheduler_task: asyncio.Task[None] | None = None
        self._active: set[str] = set()
        self._active_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(config.max_concurrent)
        self._stopping = False

    async def start(self) -> None:
        self.store.initialize()
        if self._scheduler_task and not self._scheduler_task.done():
            return
        self._stopping = False
        self._scheduler_task = asyncio.create_task(self._scheduler_loop(), name="moss-mission-scheduler")

    async def stop(self) -> None:
        self._stopping = True
        task = self._scheduler_task
        self._scheduler_task = None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def summary(self) -> dict[str, Any]:
        async with self._active_lock:
            active = len(self._active)
        return {
            "running": active,
            "counts": self.store.counts(),
            "scheduler_running": bool(self._scheduler_task and not self._scheduler_task.done()),
            "tick_seconds": self.config.tick_seconds,
            "heartbeat_seconds": self.config.heartbeat_seconds,
            "max_concurrent": self.config.max_concurrent,
            "allowed_background_risks": list(self.config.allowed_risks),
            "persistent": self.config.db_path != ":memory:",
        }

    def create(self, request: MissionCreateRequest) -> dict[str, Any]:
        for step in request.steps:
            tool = self.tools.get(step.tool)
            if tool is None:
                raise ValueError(f"unknown mission tool: {step.tool}")
            if step.tool.startswith("mission."):
                raise MissionPolicyError("mission.* tools cannot be used inside missions")
        mission = self.store.create(request)
        self.events.publish("gateway", "mission_created", self._event_summary(mission))
        return mission

    async def run_now(self, mission_id: str) -> dict[str, Any]:
        mission = self.store.get(mission_id)
        if mission["status"] == "running":
            raise MissionError("mission is already running")
        asyncio.create_task(self._execute(mission_id), name=f"moss-{mission_id}")
        return {"accepted": True, "mission_id": mission_id}

    def cancel(self, mission_id: str) -> dict[str, Any]:
        mission = self.store.get(mission_id)
        now = datetime.now(timezone.utc).isoformat()
        if mission["status"] == "running":
            updated = self.store.update(
                mission_id,
                cancel_requested=True,
                updated_at=now,
            )
        else:
            updated = self.store.update(
                mission_id,
                status="cancelled",
                enabled=False,
                next_run_at=None,
                cancel_requested=True,
                updated_at=now,
            )
        self.events.publish("gateway", "mission_cancel_requested", self._event_summary(updated))
        return updated

    def pause(self, mission_id: str) -> dict[str, Any]:
        mission = self.store.get(mission_id)
        if mission["status"] == "running":
            raise MissionError("running mission must be cancelled before pausing")
        updated = self.store.update(
            mission_id,
            status="paused",
            enabled=False,
            next_run_at=None,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        self.events.publish("gateway", "mission_paused", self._event_summary(updated))
        return updated

    def resume(self, mission_id: str) -> dict[str, Any]:
        mission = self.store.get(mission_id)
        if mission["status"] == "running":
            raise MissionError("mission is already running")
        now = datetime.now(timezone.utc)
        next_run_at: str | None = None
        if mission["interval_seconds"]:
            next_run_at = (now + timedelta(seconds=int(mission["interval_seconds"]))).isoformat()
        elif mission["run_at"]:
            configured = datetime.fromisoformat(str(mission["run_at"]))
            if configured > now:
                next_run_at = configured.isoformat()
        status = "scheduled" if next_run_at else "idle"
        updated = self.store.update(
            mission_id,
            status=status,
            enabled=True,
            next_run_at=next_run_at,
            cancel_requested=False,
            consecutive_failures=0,
            last_error=None,
            updated_at=now.isoformat(),
        )
        self.events.publish("gateway", "mission_resumed", self._event_summary(updated))
        return updated

    async def _scheduler_loop(self) -> None:
        heartbeat_elapsed = 0
        while not self._stopping:
            now = datetime.now(timezone.utc)
            for mission_id in self.store.list_due(now.isoformat()):
                async with self._active_lock:
                    already_active = mission_id in self._active
                if not already_active:
                    asyncio.create_task(self._execute(mission_id), name=f"moss-{mission_id}")
            heartbeat_elapsed += self.config.tick_seconds
            if heartbeat_elapsed >= self.config.heartbeat_seconds:
                heartbeat_elapsed = 0
                summary = await self.summary()
                self.events.publish("gateway", "mission_engine_heartbeat", summary)
            await asyncio.sleep(self.config.tick_seconds)

    async def _execute(self, mission_id: str) -> None:
        async with self._active_lock:
            if mission_id in self._active:
                return
            self._active.add(mission_id)
        try:
            async with self._semaphore:
                await self._execute_locked(mission_id)
        finally:
            async with self._active_lock:
                self._active.discard(mission_id)

    async def _execute_locked(self, mission_id: str) -> None:
        mission = self.store.get(mission_id)
        run_id = self.store.create_run(mission_id)
        now = datetime.now(timezone.utc)
        self.store.update(
            mission_id,
            status="running",
            cancel_requested=False,
            last_started_at=now.isoformat(),
            updated_at=now.isoformat(),
            last_error=None,
        )
        self.events.publish(
            "gateway",
            "mission_started",
            {"mission_id": mission_id, "run_id": run_id, "title": mission["title"]},
        )
        step_results: list[dict[str, Any]] = []
        try:
            for index, step in enumerate(mission["steps"]):
                current = self.store.get(mission_id)
                if current["cancel_requested"]:
                    raise asyncio.CancelledError
                tool_name = str(step.get("tool", ""))
                if tool_name.startswith("mission."):
                    raise MissionPolicyError("mission.* tools cannot be recursively executed")
                definition = self.tools.get(tool_name)
                if definition is None:
                    raise MissionError(f"mission tool no longer exists: {tool_name}")
                if definition.risk not in self.config.allowed_risks:
                    raise MissionPolicyError(
                        f"background mission blocked tool risk: {tool_name} ({definition.risk})"
                    )
                arguments = step.get("arguments") or {}
                if not isinstance(arguments, dict):
                    raise MissionError(f"invalid arguments for mission step {index}")
                self.events.publish(
                    "gateway",
                    "mission_step_started",
                    {"mission_id": mission_id, "run_id": run_id, "step": index, "tool": tool_name},
                )
                result = await self.tools.call(tool_name, arguments)
                step_result = {"step": index, "tool": tool_name, "result": result}
                step_results.append(step_result)
                self.events.publish(
                    "gateway",
                    "mission_step_completed",
                    {"mission_id": mission_id, "run_id": run_id, "step": index, "tool": tool_name},
                )

            finished = datetime.now(timezone.utc)
            latest = self.store.get(mission_id)
            next_run_at = None
            enabled = False
            status = "completed"
            if latest["enabled"] and latest["interval_seconds"]:
                next_run_at = (
                    finished + timedelta(seconds=int(latest["interval_seconds"]))
                ).isoformat()
                enabled = True
                status = "scheduled"
            updated = self.store.update(
                mission_id,
                status=status,
                enabled=enabled,
                next_run_at=next_run_at,
                cancel_requested=False,
                consecutive_failures=0,
                run_count=int(latest["run_count"]) + 1,
                last_finished_at=finished.isoformat(),
                updated_at=finished.isoformat(),
                last_error=None,
            )
            self.store.finish_run(run_id, status="completed", result=step_results)
            self.events.publish(
                "gateway",
                "mission_completed",
                {"mission_id": mission_id, "run_id": run_id, "next_run_at": next_run_at},
            )
            _ = updated
        except asyncio.CancelledError:
            finished = datetime.now(timezone.utc)
            self.store.update(
                mission_id,
                status="cancelled",
                enabled=False,
                next_run_at=None,
                cancel_requested=False,
                last_finished_at=finished.isoformat(),
                updated_at=finished.isoformat(),
                last_error="cancelled",
            )
            self.store.finish_run(run_id, status="cancelled", result=step_results, error="cancelled")
            self.events.publish(
                "gateway", "mission_cancelled", {"mission_id": mission_id, "run_id": run_id}
            )
        except Exception as exc:
            finished = datetime.now(timezone.utc)
            latest = self.store.get(mission_id)
            failures = int(latest["consecutive_failures"]) + 1
            error = str(exc)[:1000]
            if failures <= int(latest["max_retries"]):
                next_run_at = (
                    finished + timedelta(seconds=int(latest["retry_delay_seconds"]))
                ).isoformat()
                status = "retry_wait"
                enabled = True
            else:
                next_run_at = None
                status = "failed"
                enabled = False
            self.store.update(
                mission_id,
                status=status,
                enabled=enabled,
                next_run_at=next_run_at,
                cancel_requested=False,
                consecutive_failures=failures,
                last_finished_at=finished.isoformat(),
                updated_at=finished.isoformat(),
                last_error=error,
            )
            self.store.finish_run(run_id, status="failed", result=step_results, error=error)
            self.events.publish(
                "gateway",
                "mission_failed",
                {
                    "mission_id": mission_id,
                    "run_id": run_id,
                    "error": error,
                    "retry_at": next_run_at,
                },
            )

    @staticmethod
    def _event_summary(mission: dict[str, Any]) -> dict[str, Any]:
        return {
            "mission_id": mission["id"],
            "title": mission["title"],
            "status": mission["status"],
            "enabled": mission["enabled"],
            "next_run_at": mission["next_run_at"],
        }


def register_mission_tools(registry: ToolRegistry, engine: MissionEngine) -> None:
    registry.register(
        name="mission.list",
        description="List MOSS Gateway missions and their current scheduler state.",
        risk="read_only",
        input_schema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200}},
            "additionalProperties": False,
        },
        handler=lambda args: engine.store.list(int(args.get("limit", 100))),
    )
    registry.register(
        name="mission.get",
        description="Read one MOSS Gateway mission by id.",
        risk="read_only",
        input_schema={
            "type": "object",
            "properties": {"mission_id": {"type": "string"}},
            "required": ["mission_id"],
            "additionalProperties": False,
        },
        handler=lambda args: engine.store.get(str(args["mission_id"])),
    )


def install_mission_routes(
    app: FastAPI,
    engine: MissionEngine,
    require_admin: Callable[..., Awaitable[None]],
) -> None:
    dependencies = [Depends(require_admin)]

    @app.get("/api/v1/missions", dependencies=dependencies)
    async def list_missions(limit: int = 100) -> dict[str, Any]:
        missions = engine.store.list(limit)
        return {"missions": missions, "count": len(missions)}

    @app.post("/api/v1/missions", dependencies=dependencies)
    async def create_mission(request: MissionCreateRequest) -> dict[str, Any]:
        try:
            return engine.create(request)
        except MissionPolicyError as exc:
            raise HTTPException(status_code=403, detail=str(exc)[:500]) from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)[:500]) from None

    @app.get("/api/v1/missions/{mission_id}", dependencies=dependencies)
    async def get_mission(mission_id: str) -> dict[str, Any]:
        try:
            mission = engine.store.get(mission_id)
            runs = engine.store.list_runs(mission_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="mission not found") from None
        return {"mission": mission, "runs": runs}

    @app.post("/api/v1/missions/{mission_id}/run", dependencies=dependencies)
    async def run_mission(mission_id: str) -> dict[str, Any]:
        try:
            return await engine.run_now(mission_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="mission not found") from None
        except MissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)[:500]) from None

    @app.post("/api/v1/missions/{mission_id}/cancel", dependencies=dependencies)
    async def cancel_mission(mission_id: str) -> dict[str, Any]:
        try:
            return engine.cancel(mission_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="mission not found") from None

    @app.post("/api/v1/missions/{mission_id}/pause", dependencies=dependencies)
    async def pause_mission(mission_id: str) -> dict[str, Any]:
        try:
            return engine.pause(mission_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="mission not found") from None
        except MissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)[:500]) from None

    @app.post("/api/v1/missions/{mission_id}/resume", dependencies=dependencies)
    async def resume_mission(mission_id: str) -> dict[str, Any]:
        try:
            return engine.resume(mission_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="mission not found") from None
        except MissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)[:500]) from None
