# MOSS Device Read Bridge

## Goal

Connect Host/RDK Gateway with online ESP32 devices while keeping device execution boundaries explicit.

## Architecture

```text
Planner / Admin API
        |
        v
Gateway Tool Registry
        |
        v
Host read-only bridge allowlist
        |
        v
ESP32 Gateway WebSocket
        |
        v
ESP32 remote read allowlist
        |
        v
MCP Tool + Safety Gate
```

## Allowed remote operations

The first bridge version only exposes:

- agent status
- agent contract
- hardware profile/status
- device memory status/list/get
- safety status/classify

## Forbidden operations

The bridge does not expose:

- motor control
- infrared control
- camera capture
- memory write/remove/clear
- backend switching
- destructive actions

## Call security

Every request is bound to:

- device id
- active gateway session
- unique call id
- timeout window

A disconnected device immediately invalidates pending requests.

## Future stages

Physical control requires a separate approval flow. It will not be enabled by simply adding a new proxy tool.
