# MOSS Mission Engine

The Mission Engine runs on the Host/RDK-side MOSS Gateway. It does not run on the ESP32 and does not require a connected physical robot to create, persist, inspect or test missions.

## Responsibilities

- Persist mission definitions and run history in SQLite.
- Execute ordered Tool Registry steps.
- Support manual runs, one-shot schedules and recurring schedules.
- Support pause, resume and cancellation.
- Support bounded retries after failures.
- Publish mission lifecycle events into the Gateway EventBus.
- Emit a periodic `mission_engine_heartbeat` event.
- Recover mission state from the same SQLite database after process restarts.

## Safety policy

Background execution is intentionally fail-closed. In v0.4 the allowed background tool risks are hard-coded to:

```text
read_only
low_impact
```

Tools marked `sensitive`, `physical` or `destructive` are blocked before their callback is invoked. This means Home Assistant control tools, which are marked `physical`, are not automatically executable by the scheduler even when their entities are present in `MOSS_HA_ENTITY_ALLOWLIST`.

`mission.*` tools are also blocked inside mission steps so a mission cannot recursively create or run more missions.

This policy is not configurable through environment variables in v0.4. A future high-risk automation mode must add a separate authorization design instead of widening this default.

## Storage

Default:

```text
MOSS_MISSION_DB_PATH=./moss-gateway-data/missions.sqlite3
```

The database contains mission definitions, scheduling state and bounded run summaries. It does not store camera JPEGs or raw conversation transcripts.

Recommended RDK deployment path:

```text
MOSS_MISSION_DB_PATH=/var/lib/moss-gateway/missions.sqlite3
```

The service account must own the parent directory.

## Scheduler configuration

```text
MOSS_MISSION_TICK_SECONDS=2
MOSS_MISSION_HEARTBEAT_SECONDS=30
MOSS_MISSION_MAX_CONCURRENT=1
```

`MOSS_MISSION_MAX_CONCURRENT` is bounded to 1-4. Keeping the default at 1 makes ordering and physical-resource contention predictable while the system is still being developed.

Recurring mission intervals are bounded to 60 seconds through 7 days. A recurring mission without an explicit `run_at` starts its first scheduled run one interval in the future.

## Admin API

All mission routes require the existing Gateway admin authorization unless insecure local-development mode is explicitly enabled.

```text
GET  /api/v1/missions
POST /api/v1/missions
GET  /api/v1/missions/{mission_id}
POST /api/v1/missions/{mission_id}/run
POST /api/v1/missions/{mission_id}/cancel
POST /api/v1/missions/{mission_id}/pause
POST /api/v1/missions/{mission_id}/resume
```

Example one-shot mission:

```json
{
  "title": "gateway health check",
  "run_at": "2026-08-27T09:00:00+08:00",
  "steps": [
    {
      "tool": "gateway.health",
      "arguments": {}
    }
  ]
}
```

Example recurring mission:

```json
{
  "title": "hourly device inventory",
  "interval_seconds": 3600,
  "steps": [
    {
      "tool": "gateway.devices.list",
      "arguments": {}
    }
  ]
}
```

## Cancellation semantics

A queued mission can be cancelled immediately. A running mission checks cancellation between tool steps. v0.4 does not forcibly terminate an individual tool callback that is already executing; integrations are expected to have their own timeouts.

## Retry semantics

A mission can configure `max_retries` from 0 to 3 and `retry_delay_seconds` from 10 to 3600. A failed run enters `retry_wait` when retries remain. After the retry budget is exhausted, the mission becomes `failed` and is disabled.

## EventBus events

The engine emits lifecycle events such as:

```text
mission_created
mission_started
mission_step_started
mission_step_completed
mission_completed
mission_failed
mission_cancel_requested
mission_cancelled
mission_paused
mission_resumed
mission_engine_heartbeat
```

Payloads still pass through the Gateway's existing sanitizer before being stored in the bounded in-memory EventBus.

## Current boundary

This engine is deterministic orchestration, not an autonomous planner. It executes explicit reviewed tool steps. Natural-language goal decomposition and plan generation belong to a later MOSS Planner layer, which must submit a bounded plan into this engine rather than bypass Tool Registry risk metadata.
