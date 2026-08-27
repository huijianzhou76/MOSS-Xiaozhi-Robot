# MOSS Planner Contract

The MOSS Planner runs on the Host/RDK Gateway. It turns a user goal into a **candidate plan**, but it is deliberately not an execution engine.

The planner provider can only return structured JSON matching `moss-planner/1.0`. The Gateway then validates every step against the live `ToolRegistry` before a plan can become a Mission.

## Safety model

The planner cannot execute tools directly. There is no `/planner/execute` endpoint.

A candidate is rejected when it references an unknown tool, attempts to call `mission.*` or `planner.*`, exceeds the configured step/argument limits, or fails schema validation.

Each accepted step is annotated with the risk level registered for that tool. Only `read_only` and `low_impact` steps are currently eligible for automatic conversion into a Mission. `sensitive`, `physical`, and `destructive` steps are returned as `blocked_steps` with `requires_explicit_approval=true` and cannot be converted by `/api/v1/planner/create-mission`.

This keeps the planner from weakening the Mission Engine's background-execution policy.

## Provider contract

Configure the provider on the Host/RDK service only:

```text
MOSS_PLANNER_PROVIDER_URL=https://planner.internal/v1/plan
MOSS_PLANNER_PROVIDER_TOKEN=...
MOSS_PLANNER_TIMEOUT_SECONDS=30
MOSS_PLANNER_VERIFY_TLS=true
```

The provider receives:

```json
{
  "contract": "moss-planner/1.0",
  "goal": "...",
  "context": {},
  "constraints": {
    "max_steps": 12,
    "direct_execution": false,
    "mission_recursion": false,
    "tool_names_must_match_catalog": true
  },
  "tools": [
    {
      "name": "gateway.health",
      "description": "...",
      "risk": "read_only",
      "inputSchema": {}
    }
  ],
  "response_schema": {}
}
```

Sensitive context keys are passed through the Gateway sanitizer before leaving the service. Provider credentials are sent only in the HTTP Authorization header and are never included in the prompt payload or public status response.

The provider must return a JSON object directly or under a top-level `plan` field:

```json
{
  "plan": {
    "title": "Check system health",
    "summary": "Read the current Gateway health status.",
    "steps": [
      {
        "tool": "gateway.health",
        "arguments": {},
        "reason": "Need current health data"
      }
    ],
    "run_at": null,
    "interval_seconds": null,
    "max_retries": 0,
    "retry_delay_seconds": 30
  }
}
```

The Gateway does not accept free-form shell commands, URLs as tools, or provider-defined tool names.

## Admin API

`GET /api/v1/planner/status` reports configuration and limits without exposing credentials.

`POST /api/v1/planner/plan` sends a goal to the configured provider and returns the validated candidate with risk annotations.

`POST /api/v1/planner/validate` validates a caller-supplied structured candidate without contacting a provider.

`POST /api/v1/planner/create-mission` converts an already validated safe candidate into a persistent Mission. It refuses candidates containing any risk above `low_impact`.

## Why planning and execution are separate

The LLM is treated as an untrusted plan generator. Tool existence, risk class, size limits, recursion rules, persistence, scheduling and eventual execution are owned by deterministic Gateway code. This preserves the central safety boundary even if the provider returns malformed, over-broad or adversarial output.

No physical hardware is required for this layer.
