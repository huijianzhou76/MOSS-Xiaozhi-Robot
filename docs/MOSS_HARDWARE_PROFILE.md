# MOSS Hardware Profile

The hardware profile gives the MOSS agent a truthful description of the current ESP32 device without exposing unnecessary network identity data.

## MCP tools

### `moss.hardware.profile`

Returns relatively stable device capabilities using the existing `Board`, `SystemInfo`, `AudioCodec`, `Display` and `Camera` interfaces.

The default response includes board type/name, chip model, flash size, audio topology and sample rates, display dimensions when present, camera presence, battery/temperature capability flags and MOSS runtime capabilities.

`include_identifiers` defaults to `false`. UUID and MAC are returned only when this argument is explicitly set to `true` for a legitimate diagnostic need.

The profile never returns Wi-Fi SSID, IP address or credentials.

### `moss.hardware.status`

Returns live, privacy-reduced runtime status: free/minimum heap, audio input/output enable state, output volume, display theme/brightness when available, battery status, temperature and a sanitized network summary containing only transport type and qualitative signal level when the board exposes them.

SSID, IP, MAC and UUID are intentionally excluded from this status tool.

## Schemas

Static capability profile:

```json
{
  "schema": "moss-hardware/1.0",
  "board_type": "...",
  "identifiers_included": false,
  "chip": {},
  "audio": {},
  "display": {},
  "vision": {},
  "sensors": {},
  "capabilities": {},
  "privacy": {
    "unique_identifiers_default_redacted": true,
    "network_credentials_exposed": false
  }
}
```

Live status:

```json
{
  "schema": "moss-hardware-status/1.0",
  "board_type": "...",
  "free_heap_bytes": 0,
  "audio": {},
  "display": {},
  "battery": {},
  "thermal": {},
  "network": {
    "available": true,
    "type": "wifi",
    "signal": "strong"
  }
}
```

## Truthfulness rules

A capability is reported only from an existing board/runtime API. A camera is available only when `Board::GetCamera()` returns a real camera. A display is considered present only when its width and height are non-zero. Battery and temperature support use the board-specific boolean return values. The profile does not infer hardware from the product name or fabricate unsupported sensors.

## Agent contract

`moss-agent/1.0` now advertises `hardware_profile=true`. This means the ESP32 can describe its own device capabilities through MCP. It does not imply that remote RDK X5 hardware, Home Assistant entities or future peripherals have already been discovered.
