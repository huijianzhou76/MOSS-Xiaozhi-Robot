from __future__ import annotations

import json

from fastapi.testclient import TestClient

from moss_gateway.app import create_app
from moss_gateway.config import GatewaySettings
from moss_gateway.events import sanitize_payload


def secure_client() -> TestClient:
    app = create_app(
        GatewaySettings(
            device_token="device-secret",
            admin_token="admin-secret",
            allow_insecure=False,
        )
    )
    return TestClient(app)


def test_health_reports_security_without_exposing_tokens() -> None:
    with secure_client() as client:
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["ready"] is True
        assert body["security"] == {
            "device_auth_configured": True,
            "admin_auth_configured": True,
            "allow_insecure": False,
        }
        assert "device-secret" not in response.text
        assert "admin-secret" not in response.text


def test_admin_routes_require_token() -> None:
    with secure_client() as client:
        assert client.get("/api/v1/devices").status_code == 401
        response = client.get(
            "/api/v1/devices",
            headers={"Authorization": "Bearer admin-secret"},
        )
        assert response.status_code == 200
        assert response.json() == {"devices": [], "count": 0}


def test_device_websocket_registers_and_redacts_events() -> None:
    app = create_app(GatewaySettings(allow_insecure=True))
    with TestClient(app) as client:
        with client.websocket_connect("/ws/device") as websocket:
            websocket.send_json(
                {
                    "event": "hello",
                    "protocol": "moss-agent/1.0",
                    "device_id": "moss-lab-01",
                    "backend": "xiaozhi",
                    "board_type": "esp32s3",
                    "board_name": "MOSS LAB",
                    "session_id": "xiaozhi-session",
                    "capabilities": {"mcp": True, "runtime_state": True},
                }
            )
            welcome = websocket.receive_json()
            assert welcome["event"] == "welcome"
            assert welcome["device_id"] == "moss-lab-01"
            assert welcome["protocol"] == "moss-gateway/1.0"

            websocket.send_json(
                {
                    "event": "state",
                    "phase": "thinking",
                    "seq": 9,
                    "last_user_text": "private transcript",
                    "token": "should-never-be-stored",
                }
            )
            websocket.send_json({"event": "heartbeat", "uptime_ms": 1234})
            heartbeat_ack = websocket.receive_json()
            assert heartbeat_ack["event"] == "heartbeat_ack"

            devices = client.get("/api/v1/devices").json()["devices"]
            assert devices[0]["device_id"] == "moss-lab-01"

        events = client.get("/api/v1/events").json()["events"]
        state = next(item for item in events if item["event"] == "state")
        assert state["payload"]["phase"] == "thinking"
        assert state["payload"]["last_user_text"] == "<redacted>"
        assert state["payload"]["token"] == "<redacted>"
        assert "private transcript" not in json.dumps(events)
        assert "should-never-be-stored" not in json.dumps(events)


def test_mcp_lists_and_calls_registered_host_tools() -> None:
    with secure_client() as client:
        headers = {"Authorization": "Bearer admin-secret"}

        initialized = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert initialized.status_code == 200
        assert initialized.json()["result"]["serverInfo"]["name"] == "moss-gateway"

        listed = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ).json()
        names = {tool["name"] for tool in listed["result"]["tools"]}
        assert "gateway.health" in names
        assert "gateway.devices.list" in names

        called = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "gateway.health", "arguments": {}},
            },
        ).json()
        content = called["result"]["content"][0]
        assert content["type"] == "text"
        result = json.loads(content["text"])
        assert result["service"] == "moss-gateway"
        assert result["ready"] is True


def test_event_sanitizer_bounds_and_redacts_nested_secrets() -> None:
    clean = sanitize_payload(
        {
            "nested": {
                "Authorization": "Bearer hidden",
                "ssid": "home-wifi",
                "normal": "ok",
            }
        }
    )
    assert clean["nested"]["Authorization"] == "<redacted>"
    assert clean["nested"]["ssid"] == "<redacted>"
    assert clean["nested"]["normal"] == "ok"
