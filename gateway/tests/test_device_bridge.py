from __future__ import annotations

import asyncio

import pytest

from moss_gateway.device_commands import (
    DeviceCommandBridge,
    DeviceCommandPolicyError,
    register_device_read_tools,
)
from moss_gateway.models import DeviceHello
from moss_gateway.registry import (
    DeviceRegistry,
    DeviceToolBridgeUnavailable,
)
from moss_gateway.tools import ToolRegistry


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: asyncio.Queue[dict] = asyncio.Queue()

    async def send_json(self, message: dict) -> None:
        await self.sent.put(message)


def hello(*, bridge: bool = True) -> DeviceHello:
    return DeviceHello(
        event="hello",
        protocol="moss-agent/1.0",
        device_id="esp32-test",
        backend="moss-gateway",
        board_type="wifi",
        board_name="test-board",
        capabilities={"gateway_tool_bridge": bridge},
    )


def test_registry_round_trip_matches_call_session_and_id() -> None:
    async def scenario() -> None:
        registry = DeviceRegistry()
        socket = FakeWebSocket()
        session = await registry.register(socket, hello())

        call_task = asyncio.create_task(
            registry.call_tool(
                session.device_id,
                "moss.hardware.status",
                {},
                timeout_seconds=2,
            )
        )
        command = await asyncio.wait_for(socket.sent.get(), timeout=1)
        assert command["event"] == "tool_call"
        assert command["gateway_session_id"] == session.gateway_session_id
        assert command["name"] == "moss.hardware.status"

        stale = await registry.resolve_tool_result(
            session.device_id,
            "stale-session",
            {
                "event": "tool_result",
                "id": command["id"],
                "ok": True,
                "result": {"wrong": True},
            },
        )
        assert stale is False
        assert await registry.pending_count() == 1

        resolved = await registry.resolve_tool_result(
            session.device_id,
            session.gateway_session_id,
            {
                "event": "tool_result",
                "id": command["id"],
                "ok": True,
                "result": {"free_heap_bytes": 12345},
            },
        )
        assert resolved is True
        result = await call_task
        assert result["result"]["free_heap_bytes"] == 12345
        assert await registry.pending_count() == 0

    asyncio.run(scenario())


def test_disconnect_fails_pending_calls_immediately() -> None:
    async def scenario() -> None:
        registry = DeviceRegistry()
        socket = FakeWebSocket()
        session = await registry.register(socket, hello())
        task = asyncio.create_task(
            registry.call_tool(
                session.device_id,
                "moss.agent.get_status",
                {},
                timeout_seconds=5,
            )
        )
        await asyncio.wait_for(socket.sent.get(), timeout=1)
        await registry.unregister(session.device_id, session.gateway_session_id)
        with pytest.raises(DeviceToolBridgeUnavailable):
            await task
        assert await registry.pending_count() == 0

    asyncio.run(scenario())


def test_device_without_bridge_capability_cannot_receive_calls() -> None:
    async def scenario() -> None:
        registry = DeviceRegistry()
        socket = FakeWebSocket()
        session = await registry.register(socket, hello(bridge=False))
        with pytest.raises(DeviceToolBridgeUnavailable):
            await registry.call_tool(
                session.device_id,
                "moss.hardware.status",
                {},
                timeout_seconds=1,
            )

    asyncio.run(scenario())


def test_host_policy_blocks_physical_and_identifier_requests_before_send() -> None:
    class NeverCalledRegistry:
        called = False

        async def call_tool(self, *args, **kwargs):
            self.called = True
            raise AssertionError("host policy should reject before device send")

    async def scenario() -> None:
        registry = NeverCalledRegistry()
        bridge = DeviceCommandBridge(registry)  # type: ignore[arg-type]

        with pytest.raises(DeviceCommandPolicyError):
            await bridge.call("esp32-test", "self.motor.control", {})
        with pytest.raises(DeviceCommandPolicyError):
            await bridge.call(
                "esp32-test",
                "moss.hardware.profile",
                {"include_identifiers": True},
            )
        with pytest.raises(DeviceCommandPolicyError):
            await bridge.call(
                "esp32-test",
                "moss.safety.classify",
                {"tool_name": "self.motor.control"},
            )
        assert registry.called is False

    asyncio.run(scenario())


def test_fixed_proxy_tools_are_read_only_and_no_generic_remote_proxy_exists() -> None:
    registry = ToolRegistry()
    bridge = DeviceCommandBridge(DeviceRegistry())
    register_device_read_tools(registry, bridge)
    tools = {tool["name"]: tool for tool in registry.list()}

    expected = {
        "device.agent.status",
        "device.agent.contract",
        "device.hardware.profile",
        "device.hardware.status",
        "device.memory.status",
        "device.memory.list",
        "device.memory.get",
        "device.safety.status",
        "device.safety.classify",
    }
    assert expected <= set(tools)
    assert all(tools[name]["risk"] == "read_only" for name in expected)
    assert "device.call" not in tools
    assert "device.tool.call" not in tools
