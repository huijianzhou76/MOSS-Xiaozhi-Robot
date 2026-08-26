# MOSS Host Long-Term Memory

MOSS now has two deliberately different memory scopes:

```text
ESP32 device memory
- NVS
- up to 24 small explicit entries
- useful when the Host/RDK is unavailable

Host/RDK long-term memory
- SQLite
- larger explicit memory store
- searchable by the Gateway and future Planner
```

Neither layer automatically learns raw conversations.

## Host memory categories

The first Host/RDK schema keeps the same small semantic vocabulary as the device layer:

```text
profile
preference
fact
routine
device
```

Every entry has a stable key, category, value, explicit source label, confidence, revision and timestamps.

## Explicit-only policy

`automatic_learning` is always `false` in v0.5. Device `state` events and STT/TTS transcript fields are not copied into the long-term database. A memory is written only through an explicit authenticated API/tool call.

The memory layer also rejects keys that look like credential storage (`password`, `token`, `secret`, `api_key`, `credential`) and rejects obvious bearer tokens/private keys in values. This is a guardrail, not a general-purpose secret scanner; secrets belong in deployment secret storage, not MOSS memory.

## Storage

Default:

```text
MOSS_MEMORY_DB_PATH=./moss-gateway-data/memory.sqlite3
MOSS_MEMORY_MAX_ENTRIES=5000
```

Recommended RDK deployment path:

```text
MOSS_MEMORY_DB_PATH=/var/lib/moss-gateway/memory.sqlite3
```

The service account must own the directory. Backups of this database should be treated as user data.

## Admin API

```text
GET    /api/v1/memory/status
GET    /api/v1/memory
GET    /api/v1/memory/search?query=...
GET    /api/v1/memory/{key}
POST   /api/v1/memory
DELETE /api/v1/memory/{key}
```

Example explicit write:

```json
{
  "key": "user.drink",
  "category": "preference",
  "value": "prefers warm water",
  "source": "explicit-user-request",
  "confidence": 1.0
}
```

Writing the same key updates it and increments its revision instead of creating duplicates.

## Tool Registry

Read-only tools:

```text
memory.status
memory.list
memory.search
memory.get
```

Explicit mutation tools:

```text
memory.remember   risk=sensitive
memory.forget     risk=sensitive
```

Because Mission Engine v0.4 only permits `read_only` and `low_impact`, scheduled missions may search/read memory but cannot write or delete long-term memory in the background.

## Search

The initial implementation is deterministic local text search over a bounded recent candidate set. It does not require an embedding model or cloud vector database. A future semantic index may be added on Host/RDK, but the canonical memory record remains the SQLite entry and explicit deletion must remove the searchable representation too.

## Device synchronization

The existing ESP32 NVS memory is intentionally not overwritten from Host memory yet. Synchronization requires the Gateway-to-device command channel and conflict/revision rules. That belongs to the device bridge stage so Host and device cannot silently overwrite each other.
