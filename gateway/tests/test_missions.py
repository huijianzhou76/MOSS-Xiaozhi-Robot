from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time

from fastapi.testclient import TestClient

from moss_gateway.app import create_app
from moss_gateway.config import GatewaySettings


def mission_app(tmp_path):
    settings = GatewaySettings(
        allow_insecure=True,
        mission_db_path=str(tmp_path / "missions.sqlite3"),
        mission_tick_seconds=1,
        mission_heartbeat_seconds=5,
        mission_max_concurrent=1,
    )
    return create_app(settings)


def wait_for_status(client: TestClient, mission_id: str, expected: set[str], timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/missions/{mission_id}")
        assert response.status_code == 200
        mission = response.json()["mission"]
        if mission["status"] in expected:
            return response.json()
        time.sleep(0.05)
    raise AssertionError(f"mission {mission_id} did not reach {expected}")


def test_manual_mission_executes_allowlisted_tool_and_records_run(tmp_path) -> None:
    app = mission_app(tmp_path)
    calls: list[int] = []

    async def low_impact(arguments):
        value = int(arguments["value"])
        calls.append(value)
        return {"value": value, "ok": True}

    app.state.gateway.tools.register(
        name="test.low_impact",
        description="test low impact tool",
        risk="low_impact",
        handler=low_impact,
        input_schema={"type": "object"},
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/missions",
            json={
                "title": "manual low-impact mission",
                "enabled": False,
                "steps": [{"tool": "test.low_impact", "arguments": {"value": 7}}],
            },
        )
        assert created.status_code == 200
        mission_id = created.json()["id"]

        accepted = client.post(f"/api/v1/missions/{mission_id}/run")
        assert accepted.status_code == 200
        assert accepted.json()["accepted"] is True

        result = wait_for_status(client, mission_id, {"completed"})
        assert calls == [7]
        assert result["mission"]["run_count"] == 1
        assert result["runs"][0]["status"] == "completed"
        assert result["runs"][0]["result"][0]["tool"] == "test.low_impact"


def test_scheduler_runs_due_one_shot_mission(tmp_path) -> None:
    app = mission_app(tmp_path)
    calls: list[str] = []

    def read_tool(arguments):
        calls.append(str(arguments.get("marker")))
        return {"seen": True}

    app.state.gateway.tools.register(
        name="test.read",
        description="test read tool",
        risk="read_only",
        handler=read_tool,
        input_schema={"type": "object"},
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/missions",
            json={
                "title": "scheduled mission",
                "run_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                "steps": [{"tool": "test.read", "arguments": {"marker": "due"}}],
            },
        )
        assert created.status_code == 200
        mission_id = created.json()["id"]
        result = wait_for_status(client, mission_id, {"completed"}, timeout=4.0)
        assert calls == ["due"]
        assert result["mission"]["enabled"] is False
        assert result["mission"]["next_run_at"] is None


def test_background_mission_blocks_physical_tool_before_callback(tmp_path) -> None:
    app = mission_app(tmp_path)
    calls: list[str] = []

    def physical_tool(_):
        calls.append("executed")
        return {"ok": True}

    app.state.gateway.tools.register(
        name="test.physical",
        description="test physical tool",
        risk="physical",
        handler=physical_tool,
        input_schema={"type": "object"},
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/missions",
            json={
                "title": "must be blocked",
                "enabled": False,
                "steps": [{"tool": "test.physical", "arguments": {}}],
            },
        )
        assert created.status_code == 200
        mission_id = created.json()["id"]
        assert client.post(f"/api/v1/missions/{mission_id}/run").status_code == 200

        result = wait_for_status(client, mission_id, {"failed"})
        assert calls == []
        assert "blocked tool risk" in result["mission"]["last_error"]
        assert result["runs"][0]["status"] == "failed"


def test_mission_persists_across_gateway_restart(tmp_path) -> None:
    db_path = str(tmp_path / "persistent.sqlite3")
    settings = GatewaySettings(
        allow_insecure=True,
        mission_db_path=db_path,
        mission_tick_seconds=1,
        mission_heartbeat_seconds=5,
    )

    app1 = create_app(settings)
    with TestClient(app1) as client:
        created = client.post(
            "/api/v1/missions",
            json={
                "title": "persist me",
                "enabled": False,
                "steps": [{"tool": "gateway.health", "arguments": {}}],
            },
        )
        assert created.status_code == 200
        mission_id = created.json()["id"]

    app2 = create_app(settings)
    with TestClient(app2) as client:
        listing = client.get("/api/v1/missions")
        assert listing.status_code == 200
        ids = {item["id"] for item in listing.json()["missions"]}
        assert mission_id in ids


def test_pause_and_resume_recurring_mission(tmp_path) -> None:
    app = mission_app(tmp_path)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/missions",
            json={
                "title": "recurring",
                "enabled": True,
                "interval_seconds": 60,
                "steps": [{"tool": "gateway.health", "arguments": {}}],
            },
        )
        assert created.status_code == 200
        mission_id = created.json()["id"]
        assert created.json()["status"] == "scheduled"

        paused = client.post(f"/api/v1/missions/{mission_id}/pause")
        assert paused.status_code == 200
        assert paused.json()["status"] == "paused"
        assert paused.json()["enabled"] is False

        resumed = client.post(f"/api/v1/missions/{mission_id}/resume")
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "scheduled"
        assert resumed.json()["enabled"] is True
        assert resumed.json()["next_run_at"] is not None
