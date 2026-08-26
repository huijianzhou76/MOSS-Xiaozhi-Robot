# MOSS Vision Perception

This stage reuses the existing ESP32 camera capture/upload path instead of moving vision inference onto the MCU.

## Architecture

```text
ESP32 Camera
  -> Camera::Capture()
  -> JPEG multipart upload
  -> MOSS Gateway /api/v1/vision/explain
  -> reviewed HTTP Vision Provider
  -> bounded result JSON
  -> self.camera.take_photo result
  -> MOSS Agent
```

The ESP32 continues to own camera capture and preview. Host/RDK owns inference-provider integration.

## Device bootstrap

When Wi-Fi becomes ready, `MossVisionBootstrap` checks the persisted Agent backend. It changes camera explain configuration only when the backend is explicitly `moss-gateway`.

It reads the existing `moss_gateway` URL and device token from NVS, then derives a same-origin HTTP endpoint:

```text
ws://host:8765/ws/device
  -> http://host:8765/api/v1/vision/explain

wss://host/prefix/ws/device
  -> https://host/prefix/api/v1/vision/explain
```

The existing Gateway device credential is reused for the HTTP `Authorization` header. No second ESP32 vision credential is introduced, and the credential is not sent through Agent/MCP arguments.

Xiaozhi-only mode is not rewired and keeps the original server-provided camera explain configuration.

## Gateway endpoint

`POST /api/v1/vision/explain` uses the same multipart shape already produced by `Esp32Camera::Explain()`:

- `question`: text, 1-1000 characters
- `file`: `image/jpeg`

The route is protected by the same device authentication policy as `/ws/device`.

The gateway does not persist the image. It reads at most `MOSS_VISION_MAX_IMAGE_BYTES` and forwards the validated JPEG to a configured inference service.

## Provider contract

Configure a reviewed local/RDK/cloud adapter endpoint:

```bash
export MOSS_VISION_PROVIDER_URL='http://127.0.0.1:9000/explain'
export MOSS_VISION_PROVIDER_TOKEN='optional-provider-secret'
export MOSS_VISION_TIMEOUT_SECONDS=30
export MOSS_VISION_MAX_IMAGE_BYTES=2000000
export MOSS_VISION_VERIFY_TLS=1
```

The provider receives:

```text
POST <MOSS_VISION_PROVIDER_URL>
Authorization: Bearer <provider token>   # only when configured
Content-Type: multipart/form-data

question=<question>
file=<camera.jpg>
```

The provider must return JSON containing a non-empty string under one of:

```json
{"success": true, "result": "..."}
{"answer": "..."}
{"text": "..."}
```

A provider that reports `success=false`, returns invalid JSON, omits result text or produces an HTTP error is surfaced as a bounded Gateway error. The Gateway does not invent a vision answer when no provider is configured; the endpoint returns HTTP 503.

This HTTP provider contract is deliberately model/vendor neutral. A later branch can add a specific local VLM adapter without changing the ESP32 camera protocol.

## Privacy and security

- Gateway device authentication is required for camera uploads unless explicitly running the whole Gateway in insecure development mode.
- Provider credentials remain on Host/RDK and are never returned to the ESP32.
- Gateway health reports only configuration booleans/limits, not the provider URL or credential.
- The Gateway does not write uploaded images to disk or the event bus in this version.
- Provider URLs reject embedded `user:password`, query strings and fragments.
- The existing upstream `Esp32Camera::Explain()` currently still sends its legacy `Device-Id` (MAC) and `Client-Id` (UUID) HTTP headers. The new MOSS Gateway endpoint ignores and does not persist these identifiers; removing those legacy headers should be handled as a separate camera privacy-hardening change so it can be tested independently.

## Current limits

- This stage provides the authenticated camera-to-Host/RDK perception transport and provider abstraction; it does not bundle a neural vision model into ESP32 firmware.
- No continuous camera streaming or background surveillance is enabled.
- Vision remains user/tool initiated through the existing `self.camera.take_photo` flow.
- No face identity recognition is added by this stage.
