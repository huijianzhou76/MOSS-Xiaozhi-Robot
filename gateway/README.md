# MOSS Gateway

`gateway/` is the Host/RDK-side service layer for the MOSS Xiaozhi project. The ESP32 remains responsible for real-time device audio and hardware. The gateway owns device sessions, event routing, reviewed host-side tools and external integrations such as Home Assistant.

The gateway does **not** replace the Xiaozhi audio transport. It provides a separate `moss-agent/1.0` control/runtime plane. Current ESP32 firmware can connect to it through the independent MOSS Gateway WebSocket client while keeping Xiaozhi audio available.

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

Create separate random secrets for devices and administrators. Do not put these tokens in frontend JavaScript or commit them to Git.

```bash
export MOSS_GATEWAY_DEVICE_TOKEN='replace-with-device-secret'
export MOSS_GATEWAY_ADMIN_TOKEN='replace-with-admin-secret'
moss-gateway
```

The launcher binds to `127.0.0.1:8765` by default. Set `MOSS_GATEWAY_HOST=0.0.0.0` only when the service is intentionally reachable on a trusted LAN/VPN and authentication is configured. For remote access, terminate TLS in a trusted reverse proxy/VPN rather than exposing an unencrypted public gateway port.

`MOSS_GATEWAY_ALLOW_INSECURE=1` exists only for local development/test environments. When no gateway tokens are configured and insecure mode is not explicitly enabled, `/health` reports `configuration_required` and protected routes reject access.

Optional limits:

```text
MOSS_GATEWAY_PORT=8765
MOSS_GATEWAY_MAX_MESSAGE_BYTES=65536
MOSS_GATEWAY_HELLO_TIMEOUT_SECONDS=10
MOSS_GATEWAY_EVENT_BUFFER_SIZE=1000
```

## Home Assistant

The gateway integrates with Home Assistant through its REST API. Xiaomi Home, Matter, Zigbee and Wi-Fi device integrations remain owned by Home Assistant; MOSS does not reimplement each vendor protocol.

Create a Home Assistant long-lived access token and keep it only on the Host/RDK gateway:

```bash
export MOSS_HA_URL='http://homeassistant.local:8123'
export MOSS_HA_TOKEN='replace-with-home-assistant-long-lived-token'
export MOSS_HA_ENTITY_ALLOWLIST='light.living_room,fan.bedroom,scene.movie_time'
```

Optional settings:

```text
MOSS_HA_TIMEOUT_SECONDS=5
MOSS_HA_VERIFY_TLS=1
```

`MOSS_HA_ENTITY_ALLOWLIST` is a control allowlist, not a discovery filter. MOSS can read privacy-reduced state for supported domains, but every state-changing action is denied unless the exact `entity_id` is explicitly allowlisted by the gateway operator. An empty allowlist therefore means **read-only Home Assistant access**.

Supported domains in this first version are:

```text
light
switch
fan
climate
cover
scene
```

High-risk domains such as `lock`, alarm/security systems and gas-related devices are intentionally not exposed. There is also no generic `domain/service` proxy.

Registered Home Assistant tools include:

```text
home.status
home.entities.list
home.entity.get
home.light.turn_on
home.light.turn_off
home.light.set_brightness
home.switch.turn_on
home.switch.turn_off
home.fan.turn_on
home.fan.turn_off
home.fan.set_percentage
home.climate.set_temperature
home.cover.open
home.cover.close
home.cover.stop
home.scene.activate
```

Control tools read entity state before the service call and read it again afterward so the result contains real before/after feedback. Home Assistant access tokens are never included in tool results, device events or normal gateway health output.

## Device WebSocket

Endpoint: `/ws/device`

Authentication: `Authorization: Bearer <MOSS_GATEWAY_DEVICE_TOKEN>` or `X-MOSS-DEVICE-TOKEN`.

The first text message must be a valid `moss-agent/1.0` hello. After hello, the gateway accepts bounded `state`, `heartbeat`, `telemetry` and `tool_result` events. The in-memory event bus redacts keys containing authorization/password/secret/token/API-key/SSID/MAC/UUID and transcript fields such as `last_user_text`/`last_assistant_text`.

The ESP32-side Gateway client supports an independent WebSocket control channel, stable device ID, hello/welcome handshake, heartbeat/state synchronization, Wi-Fi auto-start and reconnect/backoff. This is separate from the Xiaozhi audio transport.

## Admin API

Admin routes accept `Authorization: Bearer <MOSS_GATEWAY_ADMIN_TOKEN>` or `X-MOSS-ADMIN-TOKEN`.

- `GET /health` — public configuration-safe health information; never returns token values
- `GET /api/v1/devices` — connected device sessions
- `GET /api/v1/events` — bounded, redacted in-memory event buffer
- `GET /api/v1/tools` — registered host-side tools and risk metadata
- `POST /api/v1/tools/call` — call an explicitly registered host tool

There is intentionally no generic shell command, arbitrary URL fetch or arbitrary Python execution tool.

## MCP JSON-RPC core

`POST /mcp` exposes the same Tool Registry through a small JSON-RPC core supporting `initialize`, `tools/list` and `tools/call`, protected by the admin token. External integrations register fixed, reviewed tools rather than hiding arbitrary actions behind a generic executor.

## Current boundaries

- Device sessions/events are memory-only and are lost when the gateway restarts.
- Home Assistant v1 uses REST polling/readback; WebSocket state subscriptions are a later stage.
- Home Assistant control is exact-entity allowlist based; empty allowlist is read-only.
- No high-risk lock/security/gas domains are exposed.
- There is no account/role system yet; the gateway uses separate device/admin bearer secrets.
- Home Assistant token storage is server-side environment configuration; production deployments should also protect the Host/RDK filesystem and process environment.
- TLS is expected to be provided by Home Assistant itself or by the deployment environment when traffic leaves localhost/trusted LAN/VPN.
