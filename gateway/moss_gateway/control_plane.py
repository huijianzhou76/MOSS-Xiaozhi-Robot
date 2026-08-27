from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


class ControlPlaneService:
    """Read-only aggregation layer for the MOSS operator console.

    The UI should consume this service instead of directly reaching into
    missions/devices/planner/safety implementations. Mutation APIs remain in
    their existing protected modules.
    """

    def __init__(self, runtime: Any):
        self.runtime = runtime

    async def dashboard(self) -> dict[str, Any]:
        return {
            "schema": "moss-control-plane/1.0",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "devices": {
                "count": await self.runtime.devices.count(),
                "items": await self.runtime.devices.list(),
            },
            "missions": await self.runtime.missions.summary(),
            "memory": self.runtime.memory.status(),
            "planner": self.runtime.planner.summary(),
            "integrations": {
                "home_assistant": {
                    "configured": self.runtime.home_assistant.configured,
                    "allowlist_count": self.runtime.home_assistant.allowlist_count,
                },
                "vision": self.runtime.vision.configuration_summary(),
                "device_bridge": self.runtime.device_commands.status(),
            },
            "events": {
                "latest_sequence": self.runtime.events.latest_sequence,
            },
        }

    async def device_overview(self, device_id: str) -> dict[str, Any]:
        devices = await self.runtime.devices.list()
        for device in devices:
            if device.get("device_id") == device_id:
                return {
                    "device": device,
                    "bridge": self.runtime.device_commands.status(),
                }
        return {
            "device": None,
            "bridge": self.runtime.device_commands.status(),
        }
