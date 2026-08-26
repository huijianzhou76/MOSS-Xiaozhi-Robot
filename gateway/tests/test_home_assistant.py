from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient

from moss_gateway.app import create_app
from moss_gateway.config import GatewaySettings
from moss_gateway.home_assistant import HomeAssistantClient, HomeAssistantConfig


def make_transport() -> httpx.MockTransport:
    states: dict[str, dict] = {
        "light.living_room": {
            "entity_id": "light.living_room",
            "state": "off",
            "attributes": {
                "friendly_name": "Living Room",
                "brightness": 0,
                "entity_picture": "/api/image/secret",
            },
        },
        "switch.coffee_machine": {
            "entity_id": "switch.coffee_machine",
            "state": "off",
            "attributes": {"friendly_name": "Coffee Machine"},
        },
        "lock.front_door": {
            "entity_id": "lock.front_door",
            "state": "locked",
            "attributes": {"friendly_name": "Front Door"},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer ha-secret"
        path = request.url.path

        if request.method == "GET" and path == "/api/":
            return httpx.Response(200, json={"message": "API running."})
        if request.method == "GET" and path == "/api/states":
            return httpx.Response(200, json=list(states.values()))
        if request.method == "GET" and path.startswith("/api/states/"):
            entity_id = path.removeprefix("/api/states/")
            state = states.get(entity_id)
            return httpx.Response(200 if state else 404, json=state or {"message": "not found"})
        if request.method == "POST" and path == "/api/services/light/turn_on":
            body = json.loads(request.content.decode("utf-8"))
            entity_id = body["entity_id"]
            states[entity_id]["state"] = "on"
            if "brightness_pct" in body:
                states[entity_id]["attributes"]["brightness"] = round(body["brightness_pct"] * 255 / 100)
            return httpx.Response(200, json=[states[entity_id]])
        if request.method == "POST" and path == "/api/services/light/turn_off":
            body = json.loads(request.content.decode("utf-8"))
            entity_id = body["entity_id"]
            states[entity_id]["state"] = "off"
            return httpx.Response(200, json=[states[entity_id]])

        return httpx.Response(404, json={"message": "unknown test endpoint"})

    return httpx.MockTransport(handler)


def ha_settings(*, allowlist: tuple[str, ...] = ()) -> GatewaySettings:
    return GatewaySettings(
        device_token="device-secret",
        admin_token="admin-secret",
        home_assistant_url="http://homeassistant.local:8123",
        home_assistant_token="ha-secret",
        home_assistant_entity_allowlist=allowlist,
    )


def test_home_status_and_entity_list_are_privacy_reduced() -> None:
    app = create_app(ha_settings(), home_assistant_transport=make_transport())
    headers = {"Authorization": "Bearer admin-secret"}

    with TestClient(app) as client:
        status = client.post(
            "/api/v1/tools/call",
            headers=headers,
            json={"name": "home.status", "arguments": {}},
        )
        assert status.status_code == 200
        body = status.json()["result"]
        assert body["configured"] is True
        assert body["reachable"] is True
        # Tool results pass through the gateway-wide sanitizer. Metadata keys
        # containing "token" are therefore redacted too; the actual secret must
        # never be returned.
        assert body["token_exposed"] == "<redacted>"
        assert body["token_configured"] == "<redacted>"
        assert "ha-secret" not in status.text

        listed = client.post(
            "/api/v1/tools/call",
            headers=headers,
            json={"name": "home.entities.list", "arguments": {}},
        )
        assert listed.status_code == 200
        entities = listed.json()["result"]["entities"]
        ids = {item["entity_id"] for item in entities}
        assert "light.living_room" in ids
        assert "switch.coffee_machine" in ids
        assert "lock.front_door" not in ids

        light = next(item for item in entities if item["entity_id"] == "light.living_room")
        assert light["controllable"] is False
        assert light["attributes"]["friendly_name"] == "Living Room"
        assert "entity_picture" not in light["attributes"]


def test_home_control_is_default_deny_without_entity_allowlist() -> None:
    app = create_app(ha_settings(), home_assistant_transport=make_transport())
    headers = {"Authorization": "Bearer admin-secret"}

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/tools/call",
            headers=headers,
            json={
                "name": "home.light.turn_on",
                "arguments": {"entity_id": "light.living_room"},
            },
        )
        assert response.status_code == 403
        assert "MOSS_HA_ENTITY_ALLOWLIST" in response.text


def test_allowlisted_light_action_calls_real_service_and_reads_back_state() -> None:
    app = create_app(
        ha_settings(allowlist=("light.living_room",)),
        home_assistant_transport=make_transport(),
    )
    headers = {"Authorization": "Bearer admin-secret"}

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/tools/call",
            headers=headers,
            json={
                "name": "home.light.set_brightness",
                "arguments": {
                    "entity_id": "light.living_room",
                    "brightness_pct": 40,
                },
            },
        )
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["service"] == "turn_on"
        assert result["before"]["state"] == "off"
        assert result["after"]["state"] == "on"
        assert result["after"]["attributes"]["brightness"] == 102
        assert result["changed_states"] == 1


def test_allowlist_is_exact_and_does_not_authorize_other_home_entities() -> None:
    app = create_app(
        ha_settings(allowlist=("light.living_room",)),
        home_assistant_transport=make_transport(),
    )
    headers = {"Authorization": "Bearer admin-secret"}

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/tools/call",
            headers=headers,
            json={
                "name": "home.switch.turn_on",
                "arguments": {"entity_id": "switch.coffee_machine"},
            },
        )
        assert response.status_code == 403


def test_mcp_exposes_named_home_tools_but_no_generic_service_proxy() -> None:
    app = create_app(ha_settings(), home_assistant_transport=make_transport())
    headers = {"Authorization": "Bearer admin-secret"}

    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        tools = response.json()["result"]["tools"]
        names = {tool["name"] for tool in tools}
        assert "home.status" in names
        assert "home.entities.list" in names
        assert "home.light.turn_on" in names
        assert "home.climate.set_temperature" in names
        assert "home.service.call" not in names
        assert "home.raw.call" not in names


def test_missing_home_tool_argument_is_invalid_params_not_unknown_tool() -> None:
    app = create_app(ha_settings(), home_assistant_transport=make_transport())
    headers = {"Authorization": "Bearer admin-secret"}

    with TestClient(app) as client:
        rest = client.post(
            "/api/v1/tools/call",
            headers=headers,
            json={"name": "home.entity.get", "arguments": {}},
        )
        assert rest.status_code == 400
        assert "missing required tool argument" in rest.text

        mcp = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "home.entity.get", "arguments": {}},
            },
        ).json()
        assert mcp["error"]["code"] == -32602


def test_home_assistant_url_rejects_embedded_credentials() -> None:
    try:
        HomeAssistantClient(
            HomeAssistantConfig(
                base_url="http://user:password@homeassistant.local:8123",
                token="ha-secret",
            )
        )
    except ValueError as exc:
        assert "embedded credentials" in str(exc)
    else:
        raise AssertionError("embedded credentials must be rejected")
