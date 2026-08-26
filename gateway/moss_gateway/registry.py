from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import WebSocket

from .events import sanitize_payload
from .models import DeviceHello


class DuplicateDeviceError(RuntimeError):
    pass


@dataclass(slots=True)
class DeviceSession:
    device_id: str
    gateway_session_id: str
    websocket: WebSocket
    hello: DeviceHello
    connected_at: str
    last_seen_at: str

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


class DeviceRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, DeviceSession] = {}
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
        async with self._lock:
            current = self._sessions.get(device_id)
            if current and current.gateway_session_id == gateway_session_id:
                self._sessions.pop(device_id, None)

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
