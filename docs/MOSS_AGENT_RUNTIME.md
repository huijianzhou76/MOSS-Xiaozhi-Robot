# MOSS Agent Runtime Wiring

This layer binds `MossAgentAdapter` to real firmware lifecycle events instead of treating agent state as a static configuration value.

## Event sources

The shared `Protocol` base class is the event source for both MQTT and WebSocket transports. It updates the MOSS agent runtime when an audio channel opens or closes, a network error occurs, listening starts/stops, STT text arrives, TTS starts, or a TTS sentence is announced.

The MCP execution path also updates the agent phase while a real tool callback is running. Tool completion restores the previous phase only if no newer runtime event changed the phase while the callback was executing.

## Runtime reconciliation

`moss.agent.get_status` reads the current `Application` snapshot before returning status:

- whether the audio channel is actually open
- the current protocol session ID
- the current device state (`listening`, `speaking`, fatal error, or idle-like states)

The adapter reconciles that snapshot with more specific protocol events. For example, an incoming STT message moves the phase to `thinking` even if the low-level audio device remains in listening mode while the server is preparing a response. A real `speaking` device state always overrides stale cached state.

## Status schema

`moss.agent.get_status` now returns structured JSON:

```json
{
  "schema": "moss-agent-state/1.0",
  "backend": "xiaozhi",
  "phase": "listening",
  "channel_open": true,
  "session_id": "...",
  "seq": 12,
  "runtime_bound": true
}
```

The public status intentionally does not return the last user or assistant transcript. Those strings are kept internally for future event/gateway work but are not exposed by the status tool.

## Phase sources

- `offline`: real audio channel closed
- `idle`: channel is open but no more specific activity is active
- `listening`: firmware sends start-listening or runtime reconciliation observes listening
- `thinking`: firmware sends stop-listening / STT arrives and the system is waiting for a response
- `speaking`: server TTS starts or the real Application device state is speaking
- `executing_tool`: a real MCP callback is running
- `error`: protocol/network error or fatal device state

Provider `tts stop` does not immediately set the phase to idle. The existing TTS engine may still be draining buffered Opus frames, so the runtime waits for the real Application state to leave speaking.

## Scope

This PR binds the existing Xiaozhi transport/runtime to the MOSS agent state contract. It does not yet implement the network transport to a separate RDK X5 / MOSS Gateway. That transport is the next independent layer and will consume the same `moss-agent/1.0` runtime contract rather than replacing the current firmware lifecycle.
