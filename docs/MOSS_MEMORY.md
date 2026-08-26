# MOSS Device Memory

`MossMemoryStore` is the ESP32-side persistent memory layer for the MOSS device runtime.

It is deliberately small and explicit. The ESP32 is not treated as the full long-term memory database for an LLM. Instead, it keeps a bounded set of user-approved facts, preferences, routines and device information in NVS so useful state survives a reboot. RDK X5 / MOSS Gateway synchronization is a later transport-layer feature.

## Storage model

- Scope: device-local only
- Backend: ESP-IDF NVS namespace `moss_memory`
- Schema: JSON document, version 1
- Maximum entries: 24
- Maximum logical key length: 40 bytes
- Maximum value length: 256 bytes
- Maximum serialized document: 3072 bytes
- Categories: `profile`, `preference`, `fact`, `routine`, `device`
- Automatic learning: disabled

The store avoids a flash write when a `set` operation does not change the existing value/category. Every real mutation advances a revision number. A malformed, oversized or unsupported on-device document is reported as unhealthy rather than being silently overwritten.

## MCP surface

`moss.memory.status` returns health and capacity information. `moss.memory.list` returns all explicitly stored entries. `moss.memory.get` reads one logical key. `moss.memory.set` writes or updates one entry. `moss.memory.remove` deletes one entry. `moss.memory.clear` clears the complete local memory and requires `confirm=\"CLEAR\"`.

The MCP descriptions explicitly warn callers not to store secrets, credentials, temporary chat transcripts or large documents in this layer.

## Agent handshake

The `moss-agent/1.0` hello capability map advertises:

```json
{
  "memory": true,
  "memory_scope": "device-local"
}
```

This is intentionally precise: it means the ESP32 has local persistent memory. It does **not** claim that an RDK X5, OpenClaw instance or remote MOSS Core memory has already been connected.

## Future gateway synchronization

A later MOSS Gateway layer can synchronize selected entries with the stronger memory service on RDK X5. That synchronization should preserve explicit consent, source/scope metadata, conflict handling and deletion semantics. The device-local memory API should remain usable even when the gateway is unavailable.
