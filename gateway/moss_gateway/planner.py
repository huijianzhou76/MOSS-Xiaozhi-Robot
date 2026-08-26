from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field, ValidationError, field_validator

from .events import EventBus
from .memory import HostMemoryStore
from .missions import MissionCreateRequest, MissionEngine, MissionStep
from .tools import ToolRegistry


class PlannerError(RuntimeError):
    pass


class PlannerStep(BaseModel):
    tool: str = Field(min_length=1, max_length=120)
    arguments: dict[str, Any] = Field(default_factory=dict)


class PlannerPlan(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=1000)
    steps: list[PlannerStep] = Field(min_length=1, max_length=12)


class PlanRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)

    @field_validator("goal")
    @classmethod
    def normalize_goal(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("goal must not be empty")
        return normalized


@dataclass(frozen=True, slots=True)
class PlannerConfig:
    provider_url: str
    provider_token: str = ""
    timeout_seconds: int = 30
    verify_tls: bool = True
    max_steps: int = 8
    include_memory: bool = False
    memory_limit: int = 6


class MossPlanner:
    def __init__(
        self,
        config: PlannerConfig,
        tools: ToolRegistry,
        memory: HostMemoryStore,
        missions: MissionEngine,
        events: EventBus,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.tools = tools
        self.memory = memory
        self.missions = missions
        self.events = events
        self._transport = transport
        self._provider_url = self._validate_url(config.provider_url) if config.provider_url else ""

    @property
    def configured(self) -> bool:
        return bool(self._provider_url)

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "provider_origin": self._provider_origin(),
            "credential_configured": bool(self.config.provider_token),
            "include_memory": self.config.include_memory,
            "max_steps": self.config.max_steps,
            "eligible_tool_risks": ["read_only", "low_impact"],
            "auto_execute": False,
            "chain_of_thought_requested": False,
        }

    def eligible_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for item in self.tools.list():
            name = str(item["name"])
            if name.startswith("mission.") or name.startswith("planner."):
                continue
            if item["risk"] not in {"read_only", "low_impact"}:
                continue
            tools.append(
                {
                    "name": name,
                    "description": str(item["description"])[:500],
                    "risk": item["risk"],
                    "inputSchema": item["inputSchema"],
                }
            )
        return tools

    async def plan(self, goal: str) -> dict[str, Any]:
        goal = PlanRequest(goal=goal).goal
        if not self.configured:
            raise PlannerError("MOSS planner provider is not configured")

        tools = self.eligible_tools()
        context: list[dict[str, Any]] = []
        if self.config.include_memory:
            context = self.memory.planner_context(goal, limit=self.config.memory_limit)

        payload = {
            "contract": "moss-planner/1.0",
            "goal": goal,
            "constraints": {
                "max_steps": self.config.max_steps,
                "allowed_risks": ["read_only", "low_impact"],
                "no_chain_of_thought": True,
                "return_only_structured_plan": True,
                "must_not_execute": True,
            },
            "tools": tools,
            "memory_context": context,
            "response_schema": {
                "title": "string",
                "summary": "short user-visible rationale summary, not chain-of-thought",
                "steps": [{"tool": "registered tool name", "arguments": {}}],
            },
        }

        headers = {"Content-Type": "application/json"}
        if self.config.provider_token:
            headers["Authorization"] = f"Bearer {self.config.provider_token}"

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(float(self.config.timeout_seconds)),
                verify=self.config.verify_tls,
                transport=self._transport,
            ) as client:
                response = await client.post(self._provider_url, headers=headers, json=payload)
        except httpx.RequestError as exc:
            raise PlannerError(f"planner provider request failed: {exc.__class__.__name__}") from exc

        if response.status_code in {401, 403}:
            raise PlannerError("planner provider authorization failed")
        if response.status_code >= 400:
            raise PlannerError(f"planner provider returned HTTP {response.status_code}")
        try:
            raw = response.json()
        except ValueError as exc:
            raise PlannerError("planner provider returned invalid JSON") from exc
        if isinstance(raw, dict) and isinstance(raw.get("plan"), dict):
            raw = raw["plan"]
        try:
            plan = PlannerPlan.model_validate(raw)
        except ValidationError as exc:
            raise PlannerError("planner provider returned an invalid plan contract") from exc

        validated = self._validate_plan(plan)
        self.events.publish(
            "gateway",
            "planner_plan_created",
            {
                "goal_chars": len(goal),
                "title": validated.title,
                "step_count": len(validated.steps),
                "tools": [step.tool for step in validated.steps],
                "memory_context_count": len(context),
            },
        )
        return {
            "contract": "moss-planner/1.0",
            "title": validated.title,
            "summary": validated.summary,
            "steps": [step.model_dump() for step in validated.steps],
            "memory_context_used": bool(context),
            "auto_execute": False,
            "validated_at": datetime.now(timezone.utc).isoformat(),
        }

    async def plan_to_disabled_mission(self, goal: str) -> dict[str, Any]:
        plan = await self.plan(goal)
        request = MissionCreateRequest(
            title=plan["title"],
            enabled=False,
            steps=[MissionStep.model_validate(step) for step in plan["steps"]],
        )
        mission = self.missions.create(request)
        self.events.publish(
            "gateway",
            "planner_mission_created",
            {
                "mission_id": mission["id"],
                "step_count": len(plan["steps"]),
                "enabled": False,
            },
        )
        return {"plan": plan, "mission": mission}

    def _validate_plan(self, plan: PlannerPlan) -> PlannerPlan:
        if len(plan.steps) > self.config.max_steps:
            raise PlannerError(
                f"planner returned {len(plan.steps)} steps; maximum is {self.config.max_steps}"
            )
        total_argument_chars = 0
        for step in plan.steps:
            if step.tool.startswith("mission.") or step.tool.startswith("planner."):
                raise PlannerError(f"planner tool is not eligible: {step.tool}")
            definition = self.tools.get(step.tool)
            if definition is None:
                raise PlannerError(f"planner referenced unknown tool: {step.tool}")
            if definition.risk not in {"read_only", "low_impact"}:
                raise PlannerError(
                    f"planner referenced blocked tool risk: {step.tool} ({definition.risk})"
                )
            total_argument_chars += len(str(step.arguments))
            if total_argument_chars > 12000:
                raise PlannerError("planner arguments exceed bounded plan size")
        return plan

    def _provider_origin(self) -> str:
        if not self._provider_url:
            return ""
        parsed = urlsplit(self._provider_url)
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{parsed.hostname}{port}"

    @staticmethod
    def _validate_url(value: str) -> str:
        raw = value.strip()
        parsed = urlsplit(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("MOSS_PLANNER_PROVIDER_URL must use http:// or https://")
        if parsed.username or parsed.password:
            raise ValueError("MOSS_PLANNER_PROVIDER_URL must not contain embedded credentials")
        if parsed.fragment:
            raise ValueError("MOSS_PLANNER_PROVIDER_URL must not contain a fragment")
        return raw


def register_planner_tools(registry: ToolRegistry, planner: MossPlanner) -> None:
    registry.register(
        name="planner.status",
        description="Read MOSS Planner provider, privacy and safety configuration.",
        risk="read_only",
        handler=lambda _: planner.status(),
    )
    registry.register(
        name="planner.plan",
        description="Generate and validate a structured plan for a goal. This sends the goal to the configured planner provider and never auto-executes.",
        risk="sensitive",
        input_schema={
            "type": "object",
            "properties": {"goal": {"type": "string"}},
            "required": ["goal"],
            "additionalProperties": False,
        },
        handler=lambda args: planner.plan(str(args["goal"])),
    )


def install_planner_routes(
    app: FastAPI,
    planner: MossPlanner,
    require_admin: Callable[..., Awaitable[None]],
) -> None:
    dependencies = [Depends(require_admin)]

    @app.get("/api/v1/planner/status", dependencies=dependencies)
    async def planner_status() -> dict[str, Any]:
        return planner.status()

    @app.post("/api/v1/planner/plan", dependencies=dependencies)
    async def create_plan(request: PlanRequest) -> dict[str, Any]:
        try:
            return await planner.plan(request.goal)
        except PlannerError as exc:
            status_code = 503 if not planner.configured else 502
            raise HTTPException(status_code=status_code, detail=str(exc)[:500]) from None

    @app.post("/api/v1/planner/mission", dependencies=dependencies)
    async def create_planned_mission(request: PlanRequest) -> dict[str, Any]:
        try:
            return await planner.plan_to_disabled_mission(request.goal)
        except PlannerError as exc:
            status_code = 503 if not planner.configured else 502
            raise HTTPException(status_code=status_code, detail=str(exc)[:500]) from None
