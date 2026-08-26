from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient

from moss_gateway.app import create_app
from moss_gateway.config import GatewaySettings


def planner_settings(tmp_path, **overrides):
    values = dict(
        allow_insecure=True,
        memory_db_path=str(tmp_path / "memory.sqlite3"),
        mission_db_path=str(tmp_path / "missions.sqlite3"),
        planner_provider_url="https://planner.local/v1/plan",
        planner_provider_token="planner-secret",
        planner_max_steps=4,
        planner_include_memory=False,
    )
    values.update(overrides)
    return GatewaySettings(**values)


def test_unconfigured_planner_is_explicitly_unavailable(tmp_path) -> None:
    app = create_app(
        GatewaySettings(
            allow_insecure=True,
            memory_db_path=str(tmp_path / "memory.sqlite3"),
            mission_db_path=str(tmp_path / "missions.sqlite3"),
        )
    )
    with TestClient(app) as client:
        status = client.get("/api/v1/planner/status")
        assert status.status_code == 200
        assert status.json()["configured"] is False
        response = client.post("/api/v1/planner/plan", json={"goal": "check system health"})
        assert response.status_code == 503


def test_planner_provider_receives_only_safe_tools_and_no_memory_by_default(tmp_path) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "title": "System health check",
                "summary": "Read the gateway health status.",
                "steps": [{"tool": "gateway.health", "arguments": {}}],
            },
        )

    app = create_app(
        planner_settings(tmp_path),
        planner_transport=httpx.MockTransport(handler),
    )
    with TestClient(app) as client:
        assert client.post(
            "/api/v1/memory",
            json={
                "key": "user.private_note",
                "category": "fact",
                "value": "health goal private context",
                "source": "explicit-test",
            },
        ).status_code == 200

        response = client.post(
            "/api/v1/planner/plan",
            json={"goal": "check system health"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["auto_execute"] is False
        assert body["memory_context_used"] is False
        assert body["steps"] == [{"tool": "gateway.health", "arguments": {}}]

    assert captured["authorization"] == "Bearer planner-secret"
    payload = captured["payload"]
    assert payload["memory_context"] == []
    assert payload["constraints"]["no_chain_of_thought"] is True
    assert payload["constraints"]["must_not_execute"] is True
    names = {tool["name"] for tool in payload["tools"]}
    assert "gateway.health" in names
    assert "memory.search" in names
    assert "memory.remember" not in names
    assert "home.light.turn_on" not in names
    assert not any(name.startswith("mission.") for name in names)
    assert not any(name.startswith("planner.") for name in names)


def test_planner_rejects_provider_plan_with_physical_tool(tmp_path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "title": "Unsafe plan",
                "summary": "Attempt physical control.",
                "steps": [{"tool": "home.light.turn_on", "arguments": {"entity_id": "light.desk"}}],
            },
        )

    app = create_app(
        planner_settings(tmp_path),
        planner_transport=httpx.MockTransport(handler),
    )
    with TestClient(app) as client:
        response = client.post("/api/v1/planner/plan", json={"goal": "turn on my desk light"})
        assert response.status_code == 502
        assert "blocked tool risk" in response.json()["detail"]
        assert client.get("/api/v1/missions").json()["count"] == 0


def test_valid_plan_can_only_create_disabled_mission(tmp_path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "title": "Inspect gateway",
                "summary": "Read health then list devices.",
                "steps": [
                    {"tool": "gateway.health", "arguments": {}},
                    {"tool": "gateway.devices.list", "arguments": {}},
                ],
            },
        )

    app = create_app(
        planner_settings(tmp_path),
        planner_transport=httpx.MockTransport(handler),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/planner/mission",
            json={"goal": "inspect gateway and devices"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["plan"]["auto_execute"] is False
        assert body["mission"]["enabled"] is False
        assert body["mission"]["status"] == "idle"
        assert [step["tool"] for step in body["mission"]["steps"]] == [
            "gateway.health",
            "gateway.devices.list",
        ]
        assert body["mission"]["run_count"] == 0


def test_memory_context_is_opt_in_and_bounded(tmp_path) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "title": "Preference-aware read",
                "summary": "Use the available read-only memory context.",
                "steps": [{"tool": "memory.search", "arguments": {"query": "warm water"}}],
            },
        )

    app = create_app(
        planner_settings(tmp_path, planner_include_memory=True, planner_memory_limit=2),
        planner_transport=httpx.MockTransport(handler),
    )
    with TestClient(app) as client:
        assert client.post(
            "/api/v1/memory",
            json={
                "key": "user.drink",
                "category": "preference",
                "value": "prefers warm water",
                "source": "explicit-user-request",
            },
        ).status_code == 200
        response = client.post(
            "/api/v1/planner/plan",
            json={"goal": "check my warm water preference"},
        )
        assert response.status_code == 200
        assert response.json()["memory_context_used"] is True

    assert len(captured["memory_context"]) == 1
    assert captured["memory_context"][0]["key"] == "user.drink"
    assert "source" not in captured["memory_context"][0]


def test_planner_tool_is_sensitive_and_cannot_run_in_background_mission(tmp_path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("planner provider must not be called by blocked mission")

    app = create_app(
        planner_settings(tmp_path),
        planner_transport=httpx.MockTransport(handler),
    )
    with TestClient(app) as client:
        tools = client.get("/api/v1/tools").json()["tools"]
        risks = {tool["name"]: tool["risk"] for tool in tools}
        assert risks["planner.plan"] == "sensitive"

        created = client.post(
            "/api/v1/missions",
            json={
                "title": "blocked planner mission",
                "enabled": False,
                "steps": [{"tool": "planner.plan", "arguments": {"goal": "do something"}}],
            },
        )
        assert created.status_code == 200
        mission_id = created.json()["id"]
        assert client.post(f"/api/v1/missions/{mission_id}/run").status_code == 200

        import time
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            mission = client.get(f"/api/v1/missions/{mission_id}").json()["mission"]
            if mission["status"] == "failed":
                break
            time.sleep(0.05)
        assert mission["status"] == "failed"
        assert "blocked tool risk" in mission["last_error"]
