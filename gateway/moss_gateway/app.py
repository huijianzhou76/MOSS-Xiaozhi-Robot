from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect, status
from pydantic import ValidationError

from .config import GatewaySettings
from .events import EventBus
from .home_assistant import (
    HomeAssistantClient,
    HomeAssistantConfig,
    HomeAssistantError,
    register_home_assistant_tools,
)
from .memory import (
    HostMemoryStore,
    MemoryConfig,
    install_memory_routes,
    register_memory_tools,
)
from .missions import (
    MissionConfig,
    MissionEngine,
    install_mission_routes,
    register_mission_tools,
)
from .models import DeviceHello, JsonRpcRequest, ToolCallRequest
from .planner import (
    MossPlanner,
    PlannerConfig,
    PlannerError,
    install_planner_routes,
    register_planner_tools,
)
from .registry import DeviceRegistry, DeviceSession, DuplicateDeviceError
from .tools import ToolRegistry
from .vision import HttpVisionProvider, VisionConfig, VisionError


_ALLOWED_DEVICE_EVENTS = {
    "state",
    "heartbeat",
    "telemetry",
    "tool_result",
}


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


def _jsonrpc_error(
    request_id: int | str | None,
    code: int,
    message: str,
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


class GatewayRuntime:
    def __init__(
        self,
        settings: GatewaySettings,
        home_assistant_transport: Any | None = None,
        vision_transport: Any | None = None,
        planner_transport: Any | None = None,
    ) -> None:
        self.settings = settings
        self.events = EventBus(settings.event_buffer_size)
        self.devices = DeviceRegistry()
        self.tools = ToolRegistry()
        self.home_assistant = HomeAssistantClient(
            HomeAssistantConfig(
                base_url=settings.home_assistant_url,
                token=settings.home_assistant_token,
                timeout_seconds=settings.home_assistant_timeout_seconds,
                verify_tls=settings.home_assistant_verify_tls,
                entity_allowlist=settings.home_assistant_entity_allowlist,
            ),
            transport=home_assistant_transport,
        )
        self.vision = HttpVisionProvider(
            VisionConfig(
                provider_url=settings.vision_provider_url,
                provider_token=settings.vision_provider_token,
                timeout_seconds=settings.vision_timeout_seconds,
                verify_tls=settings.vision_verify_tls,
                max_image_bytes=settings.vision_max_image_bytes,
            ),
            transport=vision_transport,
        )
        self.memory = HostMemoryStore(
            MemoryConfig(
                db_path=settings.memory_db_path,
                max_entries=settings.memory_max_entries,
            ),
            self.events,
        )
        self.missions = MissionEngine(
            MissionConfig(
                db_path=settings.mission_db_path,
                tick_seconds=settings.mission_tick_seconds,
                heartbeat_seconds=settings.mission_heartbeat_seconds,
                max_concurrent=settings.mission_max_concurrent,
                allowed_risks=("read_only", "low_impact"),
            ),
            self.tools,
            self.events,
        )
        self.planner = MossPlanner(
            PlannerConfig(
                provider_url=settings.planner_provider_url,
                provider_token=settings.planner_provider_token,
                timeout_seconds=settings.planner_timeout_seconds,
                verify_tls=settings.planner_verify_tls,
                max_steps=settings.planner_max_steps,
                include_memory=settings.planner_include_memory,
                memory_limit=settings.planner_memory_limit,
            ),
            self.tools,
            self.memory,
            self.missions,
            self.events,
            transport=planner_transport,
        )
        self._register_builtin_tools()
        register_home_assistant_tools(self.tools, self.home_assistant)
        register_memory_tools(self.tools, self.memory)
        register_mission_tools(self.tools, self.missions)
        register_planner_tools(self.tools, self.planner)

    def _register_builtin_tools(self) -> None:
        async def gateway_health(_: dict[str, Any]) -> dict[str, Any]:
            return await self.health()

        async def devices_list(_: dict[str, Any]) -> list[dict[str, Any]]:
            return await self.devices.list()

        self.tools.register(
            name="gateway.health",
            description="Read MOSS Gateway health and security configuration status.",
            risk="read_only",
            handler=gateway_health,
        )
        self.tools.register(
            name="gateway.devices.list",
            description="List currently connected MOSS device sessions.",
            risk="read_only",
            handler=devices_list,
        )

    async def health(self) -> dict[str, Any]:
        ready = self.settings.secure_mode or self.settings.allow_insecure
        return {
            "service": "moss-gateway",
            "version": "0.6.0",
            "status": "ok" if ready else "configuration_required",
            "ready": ready,
            "connected_devices": await self.devices.count(),
            "latest_event_seq": self.events.latest_sequence,
            "security": {
                "device_auth_configured": self.settings.device_auth_configured,
                "admin_auth_configured": self.settings.admin_auth_configured,
                "allow_insecure": self.settings.allow_insecure,
            },
            "integrations": {
                "home_assistant": {
                    "configured": self.home_assistant.configured,
                    "control_allowlist_count": self.home_assistant.allowlist_count,
                    "default_control_policy": "deny",
                },
                "vision": self.vision.configuration_summary(),
                "memory": self.memory.status(),
                "missions": await self.missions.summary(),
                "planner": self.planner.status(),
            },
        }


def create_app(
    settings: GatewaySettings | None = None,
    *,
    home_assistant_transport: Any | None = None,
    vision_transport: Any | None = None,
    planner_transport: Any | None = None,
) -> FastAPI:
    runtime = GatewayRuntime(
        settings or GatewaySettings.from_env(),
        home_assistant_transport=home_assistant_transport,
        vision_transport=vision_transport,
        planner_transport=planner_transport,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await runtime.missions.start()
        try:
            yield
        finally:
            await runtime.missions.stop()

    app = FastAPI(
        title="MOSS Gateway",
        version="0.6.0",
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.gateway = runtime

    async def require_admin(
        request: Request,
        authorization: str | None = Header(default=None),
        x_moss_admin_token: str | None = Header(default=None),
    ) -> None:
        active: GatewayRuntime = request.app.state.gateway
        presented = x_moss_admin_token or _extract_bearer(authorization)
        if not active.settings.authorize_admin(presented):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="MOSS Gateway admin authorization required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    async def require_device_http(
        request: Request,
        authorization: str | None = Header(default=None),
        x_moss_device_token: str | None = Header(default=None),
    ) -> None:
        active: GatewayRuntime = request.app.state.gateway
        presented = x_moss_device_token or _extract_bearer(authorization)
        if not active.settings.authorize_device(presented):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="MOSS Gateway device authorization required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    install_memory_routes(app, runtime.memory, require_admin)
    install_mission_routes(app, runtime.missions, require_admin)
    install_planner_routes(app, runtime.planner, require_admin)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return await runtime.health()

    @app.post(
        "/api/v1/vision/explain",
        dependencies=[Depends(require_device_http)],
    )
    async def explain_vision(
        question: str = Form(...),
        file: UploadFile = File(...),
    ) -> dict[str, Any]:
        if not runtime.vision.configured:
            raise HTTPException(
                status_code=503,
                detail="MOSS vision provider is not configured",
            )
        if file.content_type != "image/jpeg":
            await file.close()
            raise HTTPException(status_code=415, detail="vision upload must be image/jpeg")

        image = await file.read(runtime.settings.vision_max_image_bytes + 1)
        await file.close()
        if len(image) > runtime.settings.vision_max_image_bytes:
            raise HTTPException(status_code=413, detail="vision image exceeds configured size limit")

        try:
            return await runtime.vision.explain(
                question=question,
                image=image,
                content_type="image/jpeg",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)[:500]) from None
        except VisionError as exc:
            raise HTTPException(status_code=502, detail=str(exc)[:500]) from None

    @app.get("/api/v1/devices", dependencies=[Depends(require_admin)])
    async def list_devices() -> dict[str, Any]:
        devices = await runtime.devices.list()
        return {"devices": devices, "count": len(devices)}

    @app.get("/api/v1/events", dependencies=[Depends(require_admin)])
    async def list_events(since_seq: int = 0, limit: int = 200) -> dict[str, Any]:
        events = runtime.events.list(since_seq=max(0, since_seq), limit=limit)
        return {
            "events": events,
            "count": len(events),
            "latest_seq": runtime.events.latest_sequence,
        }

    @app.get("/api/v1/tools", dependencies=[Depends(require_admin)])
    async def list_tools() -> dict[str, Any]:
        tools = runtime.tools.list()
        return {"tools": tools, "count": len(tools)}

    @app.post("/api/v1/tools/call", dependencies=[Depends(require_admin)])
    async def call_tool(call: ToolCallRequest) -> dict[str, Any]:
        try:
            result = await runtime.tools.call(call.name, call.arguments)
        except KeyError as exc:
            if runtime.tools.get(call.name) is None:
                raise HTTPException(status_code=404, detail=f"unknown tool: {call.name}") from None
            missing = str(exc.args[0])[:120] if exc.args else "unknown"
            raise HTTPException(status_code=400, detail=f"missing required tool argument: {missing}") from None
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)[:500]) from None
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)[:500]) from None
        except (HomeAssistantError, PlannerError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)[:500]) from None
        return {"ok": True, "name": call.name, "result": result}

    @app.post("/mcp", dependencies=[Depends(require_admin)], response_model=None)
    async def mcp(request: JsonRpcRequest) -> Any:
        if request.id is None:
            return Response(status_code=204)

        if request.method == "initialize":
            return _jsonrpc_result(
                request.id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "moss-gateway", "version": "0.6.0"},
                },
            )

        if request.method == "tools/list":
            tools = []
            for tool in runtime.tools.list():
                tools.append(
                    {
                        "name": tool["name"],
                        "description": tool["description"],
                        "inputSchema": tool["inputSchema"],
                    }
                )
            return _jsonrpc_result(request.id, {"tools": tools})

        if request.method == "tools/call":
            name = request.params.get("name")
            arguments = request.params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, dict):
                return _jsonrpc_error(request.id, -32602, "invalid tools/call params")
            try:
                result = await runtime.tools.call(name, arguments)
            except KeyError as exc:
                if runtime.tools.get(name) is None:
                    return _jsonrpc_error(request.id, -32601, f"unknown tool: {name}")
                missing = str(exc.args[0])[:120] if exc.args else "unknown"
                return _jsonrpc_error(request.id, -32602, f"missing required tool argument: {missing}")
            except (ValueError, TypeError) as exc:
                return _jsonrpc_error(request.id, -32602, str(exc)[:500])
            except Exception as exc:
                return _jsonrpc_error(request.id, -32000, str(exc)[:500])

            text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            return _jsonrpc_result(
                request.id,
                {
                    "content": [{"type": "text", "text": text}],
                    "isError": False,
                },
            )

        return _jsonrpc_error(request.id, -32601, f"method not found: {request.method}")

    @app.websocket("/ws/device")
    async def device_socket(websocket: WebSocket) -> None:
        authorization = websocket.headers.get("authorization")
        header_token = websocket.headers.get("x-moss-device-token")
        presented = header_token or _extract_bearer(authorization)
        if not runtime.settings.authorize_device(presented):
            await websocket.close(code=1008, reason="device authorization required")
            return

        await websocket.accept()
        session: DeviceSession | None = None
        try:
            raw_hello = await asyncio.wait_for(
                websocket.receive_text(),
                timeout=runtime.settings.hello_timeout_seconds,
            )
            if len(raw_hello.encode("utf-8")) > runtime.settings.max_message_bytes:
                await websocket.close(code=1009, reason="hello too large")
                return

            try:
                hello_data = json.loads(raw_hello)
                hello = DeviceHello.model_validate(hello_data)
            except (json.JSONDecodeError, ValidationError, TypeError):
                await websocket.close(code=1008, reason="invalid moss-agent hello")
                return

            try:
                session = await runtime.devices.register(websocket, hello)
            except DuplicateDeviceError:
                await websocket.close(code=1008, reason="device already connected")
                return

            runtime.events.publish(
                session.device_id,
                "device_connected",
                hello.model_dump(mode="json"),
            )
            await websocket.send_json(
                {
                    "event": "welcome",
                    "protocol": "moss-gateway/1.0",
                    "device_id": session.device_id,
                    "gateway_session_id": session.gateway_session_id,
                    "server_time": _utc_now(),
                }
            )

            while True:
                raw = await websocket.receive_text()
                if len(raw.encode("utf-8")) > runtime.settings.max_message_bytes:
                    await websocket.close(code=1009, reason="message too large")
                    return

                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send_json(
                        {"event": "error", "code": "invalid_json"}
                    )
                    continue

                if not isinstance(message, dict):
                    await websocket.send_json(
                        {"event": "error", "code": "invalid_event"}
                    )
                    continue

                event = message.get("event")
                if event not in _ALLOWED_DEVICE_EVENTS:
                    await websocket.send_json(
                        {
                            "event": "error",
                            "code": "unsupported_event",
                            "received": str(event)[:80],
                        }
                    )
                    continue

                await runtime.devices.touch(
                    session.device_id,
                    session.gateway_session_id,
                )
                payload = dict(message)
                payload.pop("event", None)
                record = runtime.events.publish(session.device_id, event, payload)

                if event == "heartbeat":
                    await websocket.send_json(
                        {
                            "event": "heartbeat_ack",
                            "seq": record["seq"],
                            "server_time": _utc_now(),
                        }
                    )

        except asyncio.TimeoutError:
            await websocket.close(code=1008, reason="hello timeout")
        except WebSocketDisconnect:
            pass
        finally:
            if session is not None:
                await runtime.devices.unregister(
                    session.device_id,
                    session.gateway_session_id,
                )
                runtime.events.publish(
                    session.device_id,
                    "device_disconnected",
                    {"gateway_session_id": session.gateway_session_id},
                )

    return app


app = create_app()
