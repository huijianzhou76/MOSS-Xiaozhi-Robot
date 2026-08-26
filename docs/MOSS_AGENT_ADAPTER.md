# MOSS Agent Adapter

`MossAgentAdapter` is the device-side boundary between the ESP32 runtime and the AI brain.

## Design goal

The ESP32 should remain responsible for real-time device work: audio I/O, wake word, display, MCP hardware tools and board drivers. The LLM/agent brain may remain on Xiaozhi today or move to a RDK X5 / local MOSS Core later.

This branch therefore does **not** embed OpenClaw or a large model on ESP32. It introduces a stable backend identity and event contract.

## Backends

- `xiaozhi` — default. Existing Xiaozhi protocol behavior remains compatible.
- `moss-gateway` — identifies the future RDK X5 / local gateway path.

The selection is stored in NVS namespace `moss_agent`, key `backend`.

## MCP management tools

The firmware auto-registers:

- `moss.agent.get_status`
- `moss.agent.set_backend`
- `moss.agent.get_contract`

`set_backend` changes the device Agent configuration only. It does not rewrite OTA server configuration or silently redirect network traffic.

## Gateway contract

The adapter defines protocol version `moss-agent/1.0` and can build a hello envelope containing:

- backend
- board type/name
- session id
- audio capability
- MCP capability
- IoT capability
- streaming TTS support
- barge-in support

A state-event envelope is also defined for later transport integration, with phases:

`offline`, `idle`, `listening`, `thinking`, `speaking`, `executing_tool`, `error`.

## Why transport is separate

Connection endpoints are currently obtained through the existing Xiaozhi OTA/protocol configuration. Changing that behavior in the same branch would couple Agent semantics with MQTT/WebSocket transport and make regression risk much larger.

A later transport / MCP Gateway branch will connect `moss-gateway` mode to a RDK X5 or self-hosted service and send these hello/state envelopes over a dedicated controlled channel.

## Safety boundary

The Agent backend does not receive arbitrary GPIO access. Physical actions must continue through registered MCP/IoT tools, where parameter constraints and later permission/safety checks can be applied.
