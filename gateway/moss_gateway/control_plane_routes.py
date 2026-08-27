from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException
from typing import Any

from .control_plane import ControlPlaneService


def install_control_plane_routes(
    app: FastAPI,
    service: ControlPlaneService,
    require_admin: Any,
) -> None:
    dependencies = [Depends(require_admin)]

    @app.get("/api/v1/control-plane/dashboard", dependencies=dependencies)
    async def dashboard() -> dict[str, Any]:
        return await service.dashboard()

    @app.get("/api/v1/control-plane/devices", dependencies=dependencies)
    async def devices() -> dict[str, Any]:
        data = await service.dashboard()
        return data["devices"]

    @app.get("/api/v1/control-plane/devices/{device_id}", dependencies=dependencies)
    async def device(device_id: str) -> dict[str, Any]:
        result = await service.device_overview(device_id)
        if result.get("device") is None:
            raise HTTPException(status_code=404, detail="device not found")
        return result

    @app.get("/api/v1/control-plane/missions", dependencies=dependencies)
    async def missions() -> dict[str, Any]:
        return await service.runtime.missions.summary()

    @app.get("/api/v1/control-plane/planner", dependencies=dependencies)
    async def planner() -> dict[str, Any]:
        return service.runtime.planner.summary()

    @app.get("/api/v1/control-plane/home", dependencies=dependencies)
    async def home() -> dict[str, Any]:
        return {
            "configured": service.runtime.home_assistant.configured,
            "allowlist_count": service.runtime.home_assistant.allowlist_count,
        }

    @app.get("/api/v1/control-plane/vision", dependencies=dependencies)
    async def vision() -> dict[str, Any]:
        return service.runtime.vision.configuration_summary()

    @app.get("/api/v1/control-plane/events", dependencies=dependencies)
    async def events() -> dict[str, Any]:
        return {
            "latest_sequence": service.runtime.events.latest_sequence,
            "events": service.runtime.events.list(limit=100),
        }
