from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .registry import (
    DeviceRegistry,
    DeviceToolBridgeError,
    DeviceToolBridgeUnavailable,
    DeviceToolCallTimeout,
)
from .tools import ToolRegistry


class DeviceCommandError(RuntimeError):
    pass


class DeviceCommandPolicyError(DeviceCommandError):
    pass


@dataclass(frozen=True, slots=True)
class RemoteReadTool:
    remote_name: str
    description: str
    proxy_name: str | None = None


_REMOTE_READ_TOOLS: dict[str, RemoteReadTool] = {
    "moss.agent.get_status": RemoteReadTool("moss.agent.get_status", "Read the live MOSS agent state from one connected ESP32 device.", "device.agent.status"),
    "moss.agent.get_contract": RemoteReadTool("moss.agent.get_contract", "Read the MOSS agent capability contract from one connected ESP32 device.", "device.agent.contract"),
    "moss.hardware.profile": RemoteReadTool("moss.hardware.profile", "Read the privacy-aware hardware profile from one connected ESP32 device.", "device.hardware.profile"),
    "moss.hardware.status": RemoteReadTool("moss.hardware.status", "Read live hardware status from one connected ESP32 device.", "device.hardware.status"),
    "moss.memory.status": RemoteReadTool("moss.memory.status", "Read device-local memory health and capacity from one connected ESP32 device.", "device.memory.status"),
    "moss.memory.list": RemoteReadTool("moss.memory.list", "List explicit device-local memory entries from one connected ESP32 device.", "device.memory.list"),
    "moss.memory.get": RemoteReadTool("moss.memory.get", "Read one explicit device-local memory entry by key.", "device.memory.get"),
    "moss.safety.status": RemoteReadTool("moss.safety.status", "Read the ESP32 device safety-gate status without requesting authorization.", "device.safety.status"),
    "moss.safety.classify": RemoteReadTool("moss.safety.classify", "Classify one ESP32-local MCP tool name using the device safety policy.", "device.safety.classify"),
}

_ARGUMENT_FREE_TOOLS = {
    "moss.agent.get_status",
    "moss.agent.get_contract",
    "moss.hardware.profile",
    "moss.hardware.status",
    "moss.memory.status",
    "moss.memory.list",
    "moss.safety.status",
}


class DeviceToolCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    arguments: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=10.0, ge=1.0, le=30.0)


class DeviceCommandBridge:
    def __init__(self, devices: DeviceRegistry, *, max_argument_bytes: int = 4096) -> None:
        self.devices = devices
        self.max_argument_bytes = max_argument_bytes

    def status(self) -> dict[str, Any]:
        return {
            "mode": "read-only",
            "remote_allowlist_size": len(_REMOTE_READ_TOOLS),
            "max_argument_bytes": self.max_argument_bytes,
            "physical_actions": False,
            "sensitive_actions": False,
            "destructive_actions": False,
            "device_side_allowlist_required": True,
        }

    def allowed_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": definition.remote_name,
                "proxy_name": definition.proxy_name,
                "description": definition.description,
                "risk": "read_only",
            }
            for definition in _REMOTE_READ_TOOLS.values()
        ]

    def _validate_arguments(self, name: str, payload: dict[str, Any]) -> None:
        if name in _ARGUMENT_FREE_TOOLS:
            if payload:
                raise DeviceCommandPolicyError(
                    f"remote read tool does not accept arguments: {name}"
                )
            return

        if name == "moss.memory.get":
            if set(payload) != {"key"}:
                raise DeviceCommandPolicyError("moss.memory.get requires only key")
            key = payload.get("key")
            if not isinstance(key, str) or not key or len(key.encode("utf-8")) > 40:
                raise DeviceCommandPolicyError("moss.memory.get key is invalid")
            return

        if name == "moss.safety.classify":
            if set(payload) != {"tool_name"}:
                raise DeviceCommandPolicyError(
                    "moss.safety.classify requires only tool_name"
                )
            tool_name = payload.get("tool_name")
            if not isinstance(tool_name, str) or not tool_name or len(tool_name) > 120:
                raise DeviceCommandPolicyError("moss.safety.classify tool_name is invalid")
            return

        raise DeviceCommandPolicyError(
            f"remote device tool has no approved argument contract: {name}"
        )

    async def call(
        self,
        device_id: str,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout_seconds: float = 10.0,
    ) -> Any:
        definition = _REMOTE_READ_TOOLS.get(name)
        if definition is None:
            raise DeviceCommandPolicyError(
                f"remote device tool is not in read-only allowlist: {name}"
            )
        payload = arguments or {}
        self._validate_arguments(name, payload)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > self.max_argument_bytes:
            raise DeviceCommandPolicyError("remote device tool arguments exceed size limit")

        message = await self.devices.call_tool(
            device_id,
            definition.remote_name,
            payload,
            timeout_seconds=timeout_seconds,
        )
        if message.get("ok") is not True:
            error = message.get("error")
            raise DeviceCommandError(
                str(error)[:1000] if error else "device tool call failed"
            )
        return message.get("result")


def register_device_read_tools(registry: ToolRegistry, bridge: DeviceCommandBridge) -> None:
    async def call_fixed(
        args: dict[str, Any],
        remote_name: str,
        argument_keys: tuple[str, ...] = (),
    ) -> Any:
        device_id = str(args["device_id"])
        remote_arguments = {key: args[key] for key in argument_keys if key in args}
        return await bridge.call(
            device_id,
            remote_name,
            remote_arguments,
            timeout_seconds=float(args.get("timeout_seconds", 10)),
        )

    base_properties = {
        "device_id": {"type": "string", "minLength": 1, "maxLength": 80},
        "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 30},
    }

    for remote_name in _ARGUMENT_FREE_TOOLS:
        definition = _REMOTE_READ_TOOLS[remote_name]
        assert definition.proxy_name is not None
        registry.register(
            name=definition.proxy_name,
            description=definition.description,
            risk="read_only",
            input_schema={
                "type": "object",
                "properties": dict(base_properties),
                "required": ["device_id"],
                "additionalProperties": False,
            },
            handler=lambda args, target=remote_name: call_fixed(args, target),
        )

    registry.register(
        name="device.memory.get",
        description=_REMOTE_READ_TOOLS["moss.memory.get"].description,
        risk="read_only",
        input_schema={
            "type": "object",
            "properties": {
                **base_properties,
                "key": {"type": "string", "minLength": 1, "maxLength": 40},
            },
            "required": ["device_id", "key"],
            "additionalProperties": False,
        },
        handler=lambda args: call_fixed(args, "moss.memory.get", ("key",)),
    )

    registry.register(
        name="device.safety.classify",
        description=_REMOTE_READ_TOOLS["moss.safety.classify"].description,
        risk="read_only",
        input_schema={
            "type": "object",
            "properties": {
                **base_properties,
                "tool_name": {"type": "string", "minLength": 1, "maxLength": 120},
            },
            "required": ["device_id", "tool_name"],
            "additionalProperties": False,
        },
        handler=lambda args: call_fixed(args, "moss.safety.classify", ("tool_name",)),
    )


def install_device_command_routes(
    app: FastAPI,
    bridge: DeviceCommandBridge,
    require_admin: Any,
) -> None:
    dependency = [Depends(require_admin)]

    @app.get("/api/v1/device-bridge/status", dependencies=dependency)
    async def device_bridge_status() -> dict[str, Any]:
        status = bridge.status()
        status["pending_calls"] = await bridge.devices.pending_count()
        return status

    @app.get("/api/v1/device-bridge/tools", dependencies=dependency)
    async def device_bridge_tools() -> dict[str, Any]:
        tools = bridge.allowed_tools()
        return {"tools": tools, "count": len(tools)}

    @app.post("/api/v1/devices/{device_id}/tools/call", dependencies=dependency)
    async def call_device_tool(
        device_id: str,
        request: DeviceToolCallRequest,
    ) -> dict[str, Any]:
        try:
            result = await bridge.call(
                device_id,
                request.name,
                request.arguments,
                timeout_seconds=request.timeout_seconds,
            )
        except DeviceCommandPolicyError as exc:
            raise HTTPException(status_code=403, detail=str(exc)[:1000]) from None
        except DeviceToolBridgeUnavailable as exc:
            raise HTTPException(status_code=409, detail=str(exc)[:1000]) from None
        except DeviceToolCallTimeout as exc:
            raise HTTPException(status_code=504, detail=str(exc)[:1000]) from None
        except (DeviceToolBridgeError, DeviceCommandError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)[:1000]) from None
        return {
            "ok": True,
            "device_id": device_id,
            "name": request.name,
            "result": result,
        }
