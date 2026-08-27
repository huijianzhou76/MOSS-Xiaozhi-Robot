from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect, status
from pydantic import ValidationError

from .config import GatewaySettings
from .control_plane import ControlPlaneService
from .device_commands import (
    DeviceCommandBridge,
    install_device_command_routes,
    register_device_read_tools,
)
from .events import EventBus
from .home_assistant import HomeAssistantClient, HomeAssistantConfig, HomeAssistantError, register_home_assistant_tools
from .memory import HostMemoryStore, MemoryConfig, install_memory_routes, register_memory_tools
from .missions import MissionConfig, MissionEngine, install_mission_routes, register_mission_tools
from .models import DeviceHello, JsonRpcRequest, ToolCallRequest
from .planner import HttpPlannerProvider, PlannerConfig, PlannerService, install_planner_routes
from .registry import DeviceRegistry, DeviceSession, DuplicateDeviceError
from .tools import ToolRegistry
from .vision import HttpVisionProvider, VisionConfig, VisionError

_ALLOWED_DEVICE_EVENTS = {"state", "heartbeat", "telemetry", "tool_result"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_bearer(value: str | None) -> str | None:
    if not value:
        return None
    scheme, separator, token = value.partition(" ")
    if separator and scheme.lower() == "bearer":
        return token.strip() or None
    return None


def _jsonrpc_result(request_id: int | str | None, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(request_id: int | str | None, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


class GatewayRuntime:
    def __init__(self, settings: GatewaySettings, home_assistant_transport: Any | None = None, vision_transport: Any | None = None, planner_transport: Any | None = None):
        self.settings = settings
        self.events = EventBus(settings.event_buffer_size)
        self.devices = DeviceRegistry()
        self.tools = ToolRegistry()
        self.device_commands = DeviceCommandBridge(self.devices)
        self.home_assistant = HomeAssistantClient(HomeAssistantConfig(base_url=settings.home_assistant_url, token=settings.home_assistant_token, timeout_seconds=settings.home_assistant_timeout_seconds, verify_tls=settings.home_assistant_verify_tls, entity_allowlist=settings.home_assistant_entity_allowlist), transport=home_assistant_transport)
        self.vision = HttpVisionProvider(VisionConfig(provider_url=settings.vision_provider_url, provider_token=settings.vision_provider_token, timeout_seconds=settings.vision_timeout_seconds, verify_tls=settings.vision_verify_tls, max_image_bytes=settings.vision_max_image_bytes), transport=vision_transport)
        self.memory = HostMemoryStore(MemoryConfig(db_path=settings.memory_db_path, max_entries=settings.memory_max_entries), self.events)
        self.missions = MissionEngine(MissionConfig(db_path=settings.mission_db_path, tick_seconds=settings.mission_tick_seconds, heartbeat_seconds=settings.mission_heartbeat_seconds, max_concurrent=settings.mission_max_concurrent, allowed_risks=("read_only", "low_impact")), self.tools, self.events)
        self._register_builtin_tools()
        register_home_assistant_tools(self.tools, self.home_assistant)
        register_memory_tools(self.tools, self.memory)
        register_device_read_tools(self.tools, self.device_commands)
        register_mission_tools(self.tools, self.missions)
        planner_config = PlannerConfig(provider_url=settings.planner_provider_url, provider_token=settings.planner_provider_token, timeout_seconds=settings.planner_timeout_seconds, verify_tls=settings.planner_verify_tls, max_goal_chars=settings.planner_max_goal_chars, max_context_bytes=settings.planner_max_context_bytes, max_steps=settings.planner_max_steps, max_argument_bytes=settings.planner_max_argument_bytes, allowed_auto_risks=("read_only", "low_impact"))
        self.planner_provider = HttpPlannerProvider(planner_config, transport=planner_transport)
        self.planner = PlannerService(planner_config, self.planner_provider, self.tools, self.missions, self.events)
        self.control_plane = ControlPlaneService(self)

    async def health(self) -> dict[str, Any]:
        return {"service": "moss-gateway", "version": "0.8.0", "connected_devices": await self.devices.count(), "missions": await self.missions.summary(), "control_plane": True}

    def _register_builtin_tools(self) -> None:
        async def gateway_health(_: dict[str, Any]) -> dict[str, Any]:
            return await self.health()
        self.tools.register(name="gateway.health", description="Read MOSS Gateway health.", risk="read_only", handler=gateway_health)


def create_app(settings: GatewaySettings | None = None, **kwargs: Any) -> FastAPI:
    runtime = GatewayRuntime(settings or GatewaySettings.from_env(), **kwargs)
    app = FastAPI(title="MOSS Gateway", version="0.8.0")
    app.state.gateway = runtime

    async def require_admin(*_: Any, **__: Any) -> None:
        return None

    @app.get("/api/v1/control-plane/dashboard")
    async def dashboard() -> dict[str, Any]:
        return await runtime.control_plane.dashboard()

    @app.get("/api/v1/control-plane/devices/{device_id}")
    async def device_overview(device_id: str) -> dict[str, Any]:
        return await runtime.control_plane.device_overview(device_id)

    return app
