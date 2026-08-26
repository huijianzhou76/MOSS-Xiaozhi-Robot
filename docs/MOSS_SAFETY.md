# MOSS Device Safety Gate

The MOSS device safety layer is an execution-time guard for MCP tools. It is enforced inside `McpTool::Call()`, immediately before the registered callback is invoked, so every normal MCP execution path passes through the same gate.

It is not a prompt rule. A model cannot bypass a blocked physical callback simply by changing its wording.

## Standard policy

Every currently known MCP tool is reviewed by exact name. New names fall into `unknown` and fail closed until explicitly classified.

Risk levels are:

- `read_only`: status, inspection and contract reads
- `low_impact`: display/audio/lamp UI changes
- `sensitive`: camera capture, memory mutation and backend selection
- `physical`: motor and infrared control
- `destructive`: full local-memory clear
- `unknown`: any tool name not explicitly reviewed

The current `standard` policy requires local approval for `physical`, `destructive` and `unknown` tools. Known `read_only`, `low_impact` and `sensitive` tools remain available without the extra local-display challenge so normal conversation, vision and explicit memory features keep working.

`self.infrared.control` is guarded as a physical tool as a whole, including its status action, because the current firmware exposes status and transmission actions through the same MCP tool name. A later argument-aware policy can split that distinction without weakening the default.

## Local approval flow

1. A protected tool call reaches `McpTool::Call()` and is blocked before its callback executes.
2. `moss.safety.request` is called with the exact target tool name.
3. The firmware generates a random six-digit code and displays `MOSS AUTH ######` only on the physical device display.
4. The MCP response reports that a local challenge exists, but never includes the code.
5. The user reads the code from the device and explicitly provides it to `moss.safety.authorize` together with the same target tool and `confirm=CONFIRM`.
6. A one-shot grant is created for that exact tool.
7. The next matching call consumes the grant before executing. It cannot be reused.

Challenge lifetime: 60 seconds. Maximum incorrect code attempts: 5. One-shot grant lifetime: 30 seconds.

Requesting a new challenge revokes any older unconsumed grant. Pending codes and grants exist only in RAM and disappear on reboot.

## Headless devices

A protected operation cannot obtain this local approval on a board without a usable display. In that case the request is denied by default. This is intentional fail-closed behavior.

Future RDK X5 / MOSS Gateway integration can add a stronger authenticated approval channel for headless devices instead of silently bypassing the device gate.

## MCP tools

- `moss.safety.status`: current policy, pending/grant metadata and audit counters; never exposes the local code
- `moss.safety.classify`: deterministic risk classification for one tool name
- `moss.safety.request`: create a local-display challenge for a protected exact tool
- `moss.safety.authorize`: validate the physical-display code and create a one-shot grant
- `moss.safety.revoke`: clear any pending challenge and unconsumed grant

## Defense in depth

`moss.memory.clear` still requires its existing `confirm=CLEAR` argument in addition to a Safety Gate approval. The central gate is not intended to replace tool-specific validation.

Unknown tools are protected by default, which prevents a future actuator from becoming executable merely because its developer forgot to update the safety policy.

## Security boundary

This mechanism proves that someone with physical access to the device display saw a short-lived challenge. It does **not** establish an account identity and is not a cryptographic authorization protocol. Serial/debug access, compromised firmware, or a malicious local process are outside this layer's trust boundary.

The future MOSS Gateway should add authenticated users, signed requests, permission scopes and remote audit persistence while retaining this local physical-consent mechanism for high-impact actions.
