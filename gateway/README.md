# MOSS Gateway

`gateway/` is the Host/RDK-side service layer for the MOSS Xiaozhi project. The ESP32 remains responsible for real-time device audio and hardware. The gateway owns device sessions, event routing and reviewed host-side tool adapters that can later connect Home Assistant, ONVIF, PC automation and other services.

This first version does **not** replace the existing Xiaozhi audio transport. It establishes a separate `moss-agent/1.0` control/runtime plane.

## Install

Python 3.11+ is required.

```bash
cd gateway
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

For development/tests:

```bash
python -m pip install -e ".[dev]"
python -m pytest tests
```

## Secure configuration

Create separate random secrets for devices and administrators. Do not put either token in frontend JavaScript or commit it to Git.

```bash
export MOSS_GATEWAY_DEVICE_TOKEN='replace-with-device-secret'
export MOSS_GATEWAY_ADMIN_TOKEN='replace-with-admin-secret'
moss-gateway
```

The launcher binds to `127.0.0.1:8765` by default. Set `MOSS_GATEWAY_HOST=0.0.0.0` only when the service is intentionally reachable on a trusted LAN/VPN and the tokens above are configured. For remote access, terminate TLS in a trusted reverse proxy/VPN rather than exposing an unencrypted public gateway port.

`MOSS_GATEWAY_ALLOW_INSECURE=1` exists only for local development/test environments. When no tokens are configured and insecure mode is not explicitly enabled, `/health` reports `configuration_required` and protected device/admin routes reject access.

Optional limits:

```text
MOSS_GATEWAY_PORT=8765
MOSS_GATEWAY_MAX_MESSAGE_BYTES=65536
MOSS_GATEWAY_HELLO_TIMEOUT_SECONDS=10
MOSS_GATEWAY_EVENT_BUFFER_SIZE=1000
```

## Device WebSocket

Endpoint: `/ws/device`

Authentication: `Authorization: Bearer <MOSS_GATEWAY_DEVICE_TOKEN>` or `X-MOSS-DEVICE-TOKEN`.

The first text message must be a valid `moss-agent/1.0` hello, for example:

```json
{
  "event": "hello",
  "protocol": "moss-agent/1.0",
  "device_id": "moss-lab-01",
  "backend": "xiaozhi",
  "board_type": "esp32s3",
  "board_name": "MOSS LAB",
  "session_id": "current-device-session",
  "capabilities": {
    "mcp": true,
    "runtime_state": true
  }
}
```

`device_id` is optional in v0.1. If absent, the gateway creates an anonymous connection ID. A stable, privacy-preserving device identity will be added when the ESP32-to-Gateway transport is wired in a later firmware branch; MAC/SSID are not used as the default public identity.

After hello, v0.1 accepts `state`, `heartbeat`, `telemetry` and `tool_result` events. Messages are size-limited. The in-memory event bus redacts keys containing authorization/password/secret/token/API-key/SSID/MAC/UUID and transcript fields such as `last_user_text`/`last_assistant_text`.

## Admin API

Admin routes accept `Authorization: Bearer <MOSS_GATEWAY_ADMIN_TOKEN>` or `X-MOSS-ADMIN-TOKEN`.

- `GET /health` — public configuration-safe health information; never returns token values
- `GET /api/v1/devices` — connected device sessions
- `GET /api/v1/events` — bounded, redacted in-memory event buffer
- `GET /api/v1/tools` — registered host-side tools and risk metadata
- `POST /api/v1/tools/call` — call an explicitly registered host tool

There is intentionally no generic shell command, arbitrary URL fetch or arbitrary Python execution tool.

## MCP JSON-RPC core

`POST /mcp` exposes the gateway Tool Registry through a small JSON-RPC core supporting `initialize`, `tools/list` and `tools/call`, protected by the admin token. The current endpoint establishes the internal MCP tool contract but should not yet be described as a complete implementation of every MCP transport/session feature. A standards-complete external MCP transport can be layered over the same `ToolRegistry` without changing adapters.

Built-in tools are currently read-only:

```text
gateway.health
gateway.devices.list
```

Home Assistant and other real-world actions will be added as named adapters in later branches rather than being hidden behind a generic executor.

## Current boundaries

- Device sessions/events are memory-only and are lost when the gateway restarts.
- There is no ESP32 outbound Gateway WebSocket client in this PR; the existing Xiaozhi path remains unchanged.
- There is no Home Assistant adapter yet.
- There is no account/role system yet; v0.1 uses separate device/admin bearer secrets.
- TLS is expected to be provided by the deployment environment when traffic leaves localhost/trusted LAN/VPN.
