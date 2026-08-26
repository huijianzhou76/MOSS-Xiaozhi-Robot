from __future__ import annotations

import time

from fastapi.testclient import TestClient

from moss_gateway.app import create_app
from moss_gateway.config import GatewaySettings


def memory_app(tmp_path):
    return create_app(
        GatewaySettings(
            allow_insecure=True,
            memory_db_path=str(tmp_path / "memory.sqlite3"),
            mission_db_path=str(tmp_path / "missions.sqlite3"),
            mission_tick_seconds=1,
            mission_heartbeat_seconds=5,
        )
    )


def wait_for_mission(client: TestClient, mission_id: str, status: str, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/missions/{mission_id}")
        assert response.status_code == 200
        mission = response.json()["mission"]
        if mission["status"] == status:
            return mission
        time.sleep(0.05)
    raise AssertionError(f"mission did not reach {status}")


def test_explicit_memory_write_search_update_and_persistence(tmp_path) -> None:
    app = memory_app(tmp_path)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/memory",
            json={
                "key": "user.drink",
                "category": "preference",
                "value": "prefers warm water",
                "source": "explicit-user-request",
                "confidence": 1.0,
            },
        )
        assert created.status_code == 200
        assert created.json()["revision"] == 1

        found = client.get("/api/v1/memory/search", params={"query": "warm water"})
        assert found.status_code == 200
        assert found.json()["count"] == 1
        assert found.json()["memories"][0]["key"] == "user.drink"

        updated = client.post(
            "/api/v1/memory",
            json={
                "key": "user.drink",
                "category": "preference",
                "value": "prefers room-temperature water",
                "source": "explicit-user-correction",
                "confidence": 1.0,
            },
        )
        assert updated.status_code == 200
        assert updated.json()["revision"] == 2

    app2 = memory_app(tmp_path)
    with TestClient(app2) as client:
        stored = client.get("/api/v1/memory/user.drink")
        assert stored.status_code == 200
        assert stored.json()["value"] == "prefers room-temperature water"
        assert stored.json()["revision"] == 2


def test_memory_rejects_obvious_credentials(tmp_path) -> None:
    app = memory_app(tmp_path)
    with TestClient(app) as client:
        bad_key = client.post(
            "/api/v1/memory",
            json={
                "key": "service.api_token",
                "category": "fact",
                "value": "not-a-real-secret",
            },
        )
        assert bad_key.status_code == 422

        bad_value = client.post(
            "/api/v1/memory",
            json={
                "key": "service.header",
                "category": "fact",
                "value": "Bearer abcdefghijklmnopqrstuvwxyz0123456789",
            },
        )
        assert bad_value.status_code == 422


def test_device_transcripts_are_not_automatically_learned(tmp_path) -> None:
    app = memory_app(tmp_path)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/device") as websocket:
            websocket.send_json(
                {
                    "event": "hello",
                    "protocol": "moss-agent/1.0",
                    "device_id": "memory-test-device",
                    "backend": "moss-gateway",
                    "capabilities": {"mcp": True},
                }
            )
            assert websocket.receive_json()["event"] == "welcome"
            websocket.send_json(
                {
                    "event": "state",
                    "phase": "thinking",
                    "last_user_text": "remember this automatically",
                    "last_assistant_text": "I should not persist this",
                }
            )

        listing = client.get("/api/v1/memory")
        assert listing.status_code == 200
        assert listing.json()["count"] == 0
        assert client.get("/api/v1/memory/status").json()["automatic_learning"] is False


def test_mission_engine_cannot_write_sensitive_memory(tmp_path) -> None:
    app = memory_app(tmp_path)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/missions",
            json={
                "title": "blocked memory write",
                "enabled": False,
                "steps": [
                    {
                        "tool": "memory.remember",
                        "arguments": {
                            "key": "user.city",
                            "category": "profile",
                            "value": "Seoul",
                        },
                    }
                ],
            },
        )
        assert created.status_code == 200
        mission_id = created.json()["id"]
        assert client.post(f"/api/v1/missions/{mission_id}/run").status_code == 200
        mission = wait_for_mission(client, mission_id, "failed")
        assert "blocked tool risk" in mission["last_error"]
        assert client.get("/api/v1/memory").json()["count"] == 0


def test_memory_forget_is_explicit(tmp_path) -> None:
    app = memory_app(tmp_path)
    with TestClient(app) as client:
        assert client.post(
            "/api/v1/memory",
            json={"key": "device.nickname", "category": "device", "value": "MOSS desk unit"},
        ).status_code == 200
        deleted = client.delete("/api/v1/memory/device.nickname")
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True, "key": "device.nickname"}
        assert client.get("/api/v1/memory/device.nickname").status_code == 404
