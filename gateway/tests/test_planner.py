from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient

from moss_gateway.app import create_app
from moss_gateway.config import GatewaySettings


def planner_app(tmp_path, handler):
    settings = GatewaySettings(
        allow_insecure=True,
        mission_db_path=str(tmp_path / "missions.sqlite3"),
        mission_tick_seconds=1,
        mission_heartbeat_seconds=5,
        planner_provider_url="https://planner.example/v1/plan",
        planner_provider_token="planner-secret-token",
        planner_timeout_seconds=5,
        planner_max_steps=8,
    )
    return create_app(settings, planner_transport=httpx.MockTransport(handler))


def test_planner_provider_receives_redacted_context_and_bearer_token(tmp_path) -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        payload = json.loads(request.content.decode("utf-8"))
        captured["payload"] = payload
        return httpx.Response(
            200,
            json={
                "plan": {
                    "title": "Check gateway health",
                    "summary": "Read current gateway health once.",
                    "steps": [
                        {
                            "tool": "gateway.health",
                            "arguments": {},
                            "reason": "Need current health state",
                        }
                    ],
                }
            },
        )

    app = planner_app(tmp_path, handler)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/planner/plan",
            json={
                "goal": "Check whether MOSS Gateway is healthy",
                "context": {"api_token": "must-not-leak", "room": "lab"},
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["eligible_for_auto_mission"] is True
        assert body["direct_execution"] is False
        assert body["plan"]["steps"][0]["tool"] == "gateway.health"

    assert captured["authorization"] == "Bearer planner-secret-token"
    assert captured["payload"]["context"]["api_token"] == "<redacted>"
    assert captured["payload"]["context"]["room"] == "lab"
    assert captured["payload"]["constraints"]["direct_execution"] is False


def test_unknown_planner_tool_is_rejected(tmp_path) -> None:
    app = planner_app(tmp_path, lambda _: httpx.Response(200, json={}))
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/planner/validate",
            json={
                "title": "Unknown tool plan",
                "steps": [{"tool": "shell.exec", "arguments": {}}],
            },
        )
        assert response.status_code == 409
        assert "unknown planner tool" in response.json()["detail"]


def test_physical_plan_requires_approval_and_cannot_create_mission(tmp_path) -> None:
    app = planner_app(tmp_path, lambda _: httpx.Response(200, json={}))
    calls = []

    def physical(_):
        calls.append("executed")
        return {"ok": True}

    app.state.gateway.tools.register(
        name="test.physical",
        description="test physical actuator",
        risk="physical",
        handler=physical,
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    candidate = {
        "title": "Move actuator",
        "steps": [{"tool": "test.physical", "arguments": {}, "reason": "requested"}],
    }

    with TestClient(app) as client:
        validated = client.post("/api/v1/planner/validate", json=candidate)
        assert validated.status_code == 200
        body = validated.json()
        assert body["eligible_for_auto_mission"] is False
        assert body["requires_explicit_approval"] is True
        assert body["blocked_steps"][0]["risk"] == "physical"

        created = client.post("/api/v1/planner/create-mission", json=candidate)
        assert created.status_code == 403
        assert "requires explicit approval" in created.json()["detail"]
        assert calls == []


def test_safe_plan_can_be_converted_to_persistent_mission(tmp_path) -> None:
    app = planner_app(tmp_path, lambda _: httpx.Response(200, json={}))
    candidate = {
        "title": "Health audit",
        "summary": "Read gateway health safely.",
        "steps": [{"tool": "gateway.health", "arguments": {}, "reason": "audit"}],
        "max_retries": 1,
        "retry_delay_seconds": 30,
    }

    with TestClient(app) as client:
        created = client.post("/api/v1/planner/create-mission", json=candidate)
        assert created.status_code == 200
        mission = created.json()
        assert mission["title"] == "Health audit"
        assert mission["steps"][0]["tool"] == "gateway.health"
        assert mission["status"] == "idle"

        listing = client.get("/api/v1/missions")
        assert listing.status_code == 200
        assert mission["id"] in {item["id"] for item in listing.json()["missions"]}


def test_planner_recursion_is_rejected(tmp_path) -> None:
    app = planner_app(tmp_path, lambda _: httpx.Response(200, json={}))
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/planner/validate",
            json={
                "title": "recursive",
                "steps": [{"tool": "mission.list", "arguments": {}}],
            },
        )
        assert response.status_code == 409
        assert "recursive planner/mission tool" in response.json()["detail"]
