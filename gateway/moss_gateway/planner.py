from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .events import EventBus, sanitize_payload
from .missions import MissionCreateRequest, MissionEngine, MissionStep
from .tools import ToolRegistry


class PlannerError(RuntimeError):
    pass


class PlannerPolicyError(PlannerError):
    pass


class PlannerStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1, max_length=120)
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=500)


class PlannerCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(default="", max_length=1000)
    steps: list[PlannerStep] = Field(min_length=1, max_length=20)
    run_at: datetime | None = None
    interval_seconds: int | None = Field(default=None, ge=60, le=604800)
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


class PlannerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=4000)
    context: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlannerConfig:
    provider_url: str = ""
    provider_token: str = ""
    timeout_seconds: int = 30
    verify_tls: bool = True
    max_goal_chars: int = 4000
    max_context_bytes: int = 16000
    max_steps: int = 12
    max_argument_bytes: int = 4096
    allowed_auto_risks: tuple[str, ...] = ("read_only", "low_impact")


class HttpPlannerProvider:
    """Generic structured-planning provider.

    The configured endpoint receives a JSON planning contract and must return a
    JSON object matching PlannerCandidate, either directly or under `plan`.
    No provider-specific tool execution protocol is accepted here.
    """

    def __init__(self, config: PlannerConfig, transport: Any | None = None) -> None:
        self.config = config
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.config.provider_url)

    def summary(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "timeout_seconds": self.config.timeout_seconds,
            "verify_tls": self.config.verify_tls,
            "provider_credential_configured": bool(self.config.provider_token),
        }

    async def plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            raise PlannerError("MOSS planner provider is not configured")

        headers = {"Content-Type": "application/json"}
        if self.config.provider_token:
            headers["Authorization"] = f"Bearer {self.config.provider_token}"

        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self.config.timeout_seconds,
                verify=self.config.verify_tls,
            ) as client:
                response = await client.post(
                    self.config.provider_url,
                    headers=headers,
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise PlannerError(f"planner provider request failed: {exc}") from None

        if response.status_code < 200 or response.status_code >= 300:
            raise PlannerError(
                f"planner provider returned HTTP {response.status_code}"
            )
        try:
            data = response.json()
        except ValueError:
            raise PlannerError("planner provider returned invalid JSON") from None
        if not isinstance(data, dict):
            raise PlannerError("planner provider response must be a JSON object")
        candidate = data.get("plan", data)
        if not isinstance(candidate, dict):
            raise PlannerError("planner provider `plan` must be a JSON object")
        return candidate


class PlannerService:
    def __init__(
        self,
        config: PlannerConfig,
        provider: HttpPlannerProvider,
        tools: ToolRegistry,
        missions: MissionEngine,
        events: EventBus,
    ) -> None:
        self.config = config
        self.provider = provider
        self.tools = tools
        self.missions = missions
        self.events = events

    def summary(self) -> dict[str, Any]:
        return {
            "provider": self.provider.summary(),
            "max_steps": self.config.max_steps,
            "max_argument_bytes": self.config.max_argument_bytes,
            "allowed_auto_mission_risks": list(self.config.allowed_auto_risks),
            "direct_execution": False,
        }

    def _tool_catalog(self) -> list[dict[str, Any]]:
        catalog: list[dict[str, Any]] = []
        for definition in self.tools.list():
            name = str(definition["name"])
            if name.startswith("mission.") or name.startswith("planner."):
                continue
            catalog.append(
                {
                    "name": name,
                    "description": definition["description"],
                    "risk": definition["risk"],
                    "inputSchema": definition["inputSchema"],
                }
            )
        return catalog

    async def propose(self, request: PlannerRequest) -> dict[str, Any]:
        goal = request.goal.strip()
        if len(goal) > self.config.max_goal_chars:
            raise ValueError("planner goal exceeds configured size limit")
        context_size = len(
            json.dumps(request.context, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if context_size > self.config.max_context_bytes:
            raise ValueError("planner context exceeds configured size limit")

        contract = {
            "contract": "moss-planner/1.0",
            "goal": goal,
            "context": sanitize_payload(request.context),
            "constraints": {
                "max_steps": self.config.max_steps,
                "direct_execution": False,
                "mission_recursion": False,
                "tool_names_must_match_catalog": True,
            },
            "tools": self._tool_catalog(),
            "response_schema": {
                "title": "string",
                "summary": "string",
                "steps": [{"tool": "string", "arguments": {}, "reason": "string"}],
                "run_at": "timezone-aware ISO-8601 or null",
                "interval_seconds": "integer >= 60 or null",
                "max_retries": "0..3",
                "retry_delay_seconds": "10..3600",
            },
        }
        raw = await self.provider.plan(contract)
        try:
            candidate = PlannerCandidate.model_validate(raw)
        except Exception as exc:
            raise PlannerError(f"planner candidate failed schema validation: {exc}") from None
        result = self.validate(candidate)
        self.events.publish(
            "gateway",
            "planner_candidate_created",
            {
                "title": candidate.title,
                "step_count": len(candidate.steps),
                "eligible_for_auto_mission": result["eligible_for_auto_mission"],
                "risks": result["risks"],
            },
        )
        return result

    def validate(self, candidate: PlannerCandidate) -> dict[str, Any]:
        if len(candidate.steps) > self.config.max_steps:
            raise PlannerPolicyError(
                f"planner candidate exceeds max steps ({self.config.max_steps})"
            )

        steps: list[dict[str, Any]] = []
        risk_counts: dict[str, int] = {}
        blocked: list[dict[str, Any]] = []
        for index, step in enumerate(candidate.steps):
            if step.tool.startswith("mission.") or step.tool.startswith("planner."):
                raise PlannerPolicyError(
                    f"recursive planner/mission tool is not allowed: {step.tool}"
                )
            definition = self.tools.get(step.tool)
            if definition is None:
                raise PlannerPolicyError(f"unknown planner tool: {step.tool}")
            argument_size = len(
                json.dumps(step.arguments, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
            if argument_size > self.config.max_argument_bytes:
                raise PlannerPolicyError(
                    f"planner step {index} arguments exceed size limit"
                )
            risk_counts[definition.risk] = risk_counts.get(definition.risk, 0) + 1
            if definition.risk not in self.config.allowed_auto_risks:
                blocked.append(
                    {"step": index, "tool": step.tool, "risk": definition.risk}
                )
            steps.append(
                {
                    "tool": step.tool,
                    "arguments": sanitize_payload(step.arguments),
                    "reason": step.reason,
                    "risk": definition.risk,
                }
            )

        return {
            "contract": "moss-planner/1.0",
            "plan": {
                "title": candidate.title,
                "summary": candidate.summary,
                "steps": steps,
                "run_at": candidate.run_at.isoformat() if candidate.run_at else None,
                "interval_seconds": candidate.interval_seconds,
                "max_retries": candidate.max_retries,
                "retry_delay_seconds": candidate.retry_delay_seconds,
            },
            "risks": risk_counts,
            "blocked_steps": blocked,
            "eligible_for_auto_mission": not blocked,
            "requires_explicit_approval": bool(blocked),
            "direct_execution": False,
        }

    def create_mission(self, candidate: PlannerCandidate) -> dict[str, Any]:
        validated = self.validate(candidate)
        if not validated["eligible_for_auto_mission"]:
            blocked = ", ".join(
                f"{item['tool']}({item['risk']})" for item in validated["blocked_steps"]
            )
            raise PlannerPolicyError(
                f"planner candidate requires explicit approval before mission creation: {blocked}"
            )

        request = MissionCreateRequest(
            title=candidate.title,
            steps=[
                MissionStep(tool=step.tool, arguments=step.arguments)
                for step in candidate.steps
            ],
            run_at=candidate.run_at,
            interval_seconds=candidate.interval_seconds,
            enabled=True,
            max_retries=candidate.max_retries,
            retry_delay_seconds=candidate.retry_delay_seconds,
        )
        mission = self.missions.create(request)
        self.events.publish(
            "gateway",
            "planner_mission_created",
            {"mission_id": mission["id"], "title": mission["title"]},
        )
        return mission


def install_planner_routes(
    app: FastAPI,
    planner: PlannerService,
    require_admin: Any,
) -> None:
    dependency = [Depends(require_admin)]

    @app.get("/api/v1/planner/status", dependencies=dependency)
    async def planner_status() -> dict[str, Any]:
        return planner.summary()

    @app.post("/api/v1/planner/plan", dependencies=dependency)
    async def planner_plan(request: PlannerRequest) -> dict[str, Any]:
        if not planner.provider.configured:
            raise HTTPException(status_code=503, detail="MOSS planner provider is not configured")
        try:
            return await planner.propose(request)
        except PlannerPolicyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)[:1000]) from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)[:1000]) from None
        except PlannerError as exc:
            raise HTTPException(status_code=502, detail=str(exc)[:1000]) from None

    @app.post("/api/v1/planner/validate", dependencies=dependency)
    async def planner_validate(candidate: PlannerCandidate) -> dict[str, Any]:
        try:
            return planner.validate(candidate)
        except PlannerPolicyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)[:1000]) from None

    @app.post("/api/v1/planner/create-mission", dependencies=dependency)
    async def planner_create_mission(candidate: PlannerCandidate) -> dict[str, Any]:
        try:
            return planner.create_mission(candidate)
        except PlannerPolicyError as exc:
            raise HTTPException(status_code=403, detail=str(exc)[:1000]) from None
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)[:1000]) from None
