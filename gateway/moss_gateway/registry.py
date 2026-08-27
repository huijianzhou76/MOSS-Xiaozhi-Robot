from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import WebSocket

from .events import sanitize_payload
from .models import DeviceHello


class DuplicateDeviceError(RuntimeError):
    pass


class DeviceToolBridgeError(RuntimeError):
    pass


class DeviceToolBridgeUnavailable(DeviceToolBridgeError):
    pass


class DeviceToolCallTimeout(DeviceToolBridgeError):
    pass


@dataclass(slots=True)
class DeviceSession:
    device_id: str
    gateway_session_id: str
    websocket: WebSocket
    hello: DeviceHello
    connected_at: str
    last_seen_at: str
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def summary(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "gateway_session_id": self.gateway_session_id,
            "backend": self.hello.backend,
            "board_type": self.hello.board_type,
            "board_name": self.hello.board_name,
            "device_session_id": self.hello.session_id,
            "capabilities": sanitize_payload(self.hello.capabilities),
            "connected_at": self.connected_at,
            "last_seen_at": self.last_seen_at,
        }


@dataclass(slots=True)
class PendingDeviceToolCall:
    call_id: str
    device_id: str
    gateway_session_id: str
    future: asyncio.Future[dict[str, Any]]


class DeviceRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, DeviceSession] = {}
        self._pending_calls: dict[str, PendingDeviceToolCall] = {}
        self._lock = asyncio.Lock()

    async def register(self, websocket: WebSocket, hello: DeviceHello) -> DeviceSession:
        async with self._lock:
            device_id = hello.device_id or f"anon-{uuid4().hex[:12]}"
            if device_id in self._sessions:
                raise DuplicateDeviceError(f"device already connected: {device_id}")

            now = datetime.now(timezone.utc).isoformat()
            session = DeviceSession(
                device_id=device_id,
                gateway_session_id=uuid4().hex,
                websocket=websocket,
                hello=hello,
                connected_at=now,
                last_seen_at=now,
            )
            self._sessions[device_id] = session
            return session

    async def unregister(self, device_id: str, gateway_session_id: str) -> None:
        failed: list[asyncio.Future[dict[str, Any]]] = []
        async with self._lock:
            current = self._sessions.get(device_id)
            if current and current.gateway_session_id == gateway_session_id:
                self._sessions.pop(device_id, None)
                for call_id, pending in list(self._pending_calls.items()):
                    if (
                        pending.device_id == device_id
                        and pending.gateway_session_id == gateway_session_id
                    ):
                        self._pending_calls.pop(call_id, None)
                        failed.append(pending.future)
        for future in failed:
            if not future.done():
                future.set_exception(
                    DeviceToolBridgeUnavailable("device disconnected during tool call")
                )

    async def touch(self, device_id: str, gateway_session_id: str) -> None:
        async with self._lock:
            current = self._sessions.get(device_id)
            if current and current.gateway_session_id == gateway_session_id:
                current.last_seen_at = datetime.now(timezone.utc).isoformat()

    async def list(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [session.summary() for session in self._sessions.values()]

    async def count(self) -> int:
        async with self._lock:
            return len(self._sessions)

    async def pending_count(self) -> int:
        async with self._lock:
            return len(self._pending_calls)

    async def call_tool(
        self,
        device_id: str,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float = 10.0,
    ) -> dict[str, Any]:
        async with self._lock:
            session = self._sessions.get(device_id)
            if session is None:
                raise DeviceToolBridgeUnavailable(f"device is not connected: {device_id}")
            if session.hello.capabilities.get("gateway_tool_bridge") is not True:
                raise DeviceToolBridgeUnavailable(
                    f"device does not advertise gateway_tool_bridge: {device_id}"
                )
            call_id = f"call_{uuid4().hex}"
            future: asyncio.Future[dict[str, Any]] = (
                asyncio.get_running_loop().create_future()
            )
            self._pending_calls[call_id] = PendingDeviceToolCall(
                call_id=call_id,
                device_id=device_id,
                gateway_session_id=session.gateway_session_id,
                future=future,
            )
            gateway_session_id = session.gateway_session_id

        message = {
            "event": "tool_call",
            "id": call_id,
            "gateway_session_id": gateway_session_id,
            "name": name,
            "arguments": arguments,
        }
        try:
            async with session.send_lock:
                await session.websocket.send_json(message)
        except Exception as exc:
            await self._remove_pending(call_id, future)
            raise DeviceToolBridgeUnavailable(
                f"failed to send device tool call: {exc}"
            ) from None

        try:
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except asyncio.TimeoutError:
            await self._remove_pending(call_id, future)
            raise DeviceToolCallTimeout(
                f"device tool call timed out after {timeout_seconds:g}s"
            ) from None
        finally:
            await self._remove_pending(call_id, future)

    async def resolve_tool_result(
        self,
        device_id: str,
        gateway_session_id: str,
        message: dict[str, Any],
    ) -> bool:
        call_id = message.get("id")
        if not isinstance(call_id, str) or not call_id:
            return False

        async with self._lock:
            pending = self._pending_calls.get(call_id)
            if pending is None:
                return False
            if (
                pending.device_id != device_id
                or pending.gateway_session_id != gateway_session_id
            ):
                return False
            self._pending_calls.pop(call_id, None)
            future = pending.future

        if not future.done():
            future.set_result(sanitize_payload(message))
        return True

    async def _remove_pending(
        self,
        call_id: str,
        future: asyncio.Future[dict[str, Any]],
    ) -> None:
        async with self._lock:
            current = self._pending_calls.get(call_id)
            if current is not None and current.future is future:
                self._pending_calls.pop(call_id, None)
