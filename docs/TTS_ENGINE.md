# MOSS TTS Engine

This branch introduces the first device-side TTS lifecycle layer for MOSS.

## Why this exists

The original firmware already receives `tts` JSON events and Opus audio from the Xiaozhi server, but playback state is coupled directly to `Application`. That makes it difficult to add a different voice provider later and can also make interruption/draining behavior fragile.

The new `main/speech/tts_engine.h` keeps synthesis and playback lifecycle separate.

## Current provider

`XiaozhiServer` is the only provider wired into the live firmware in this phase. It remains the source of TTS audio, so this change does not require a new cloud service and does not break the existing protocol.

The engine already defines two future source types:

- `ExternalStream` for RDK X5 / local gateway / cloud TTS streams.
- `LocalAsset` for local system prompts and alarms.

Those future source types are interfaces only in this phase; no claim is made that local neural TTS already runs on ESP32.

## Lifecycle

```text
Idle
  -> Streaming      provider starts an utterance
  -> Draining       provider has sent its final packet
  -> Idle           decode queue and decoder worker are both empty

Streaming/Draining
  -> Interrupted    wake word, user action or higher-priority speech interrupts
```

The important change is that a provider `stop` event no longer means "audio has finished playing". It means only "no more packets are coming". MOSS returns to listening only after playback has actually drained.

## Priority model

The device-side model reserves three levels:

- `Chat` (10): ordinary assistant replies.
- `System` (50): device/system prompts.
- `Alarm` (100): critical warnings and safety events.

A lower-priority utterance cannot replace a higher-priority active utterance. Equal or higher priority can preempt it. In this first integration only Xiaozhi chat TTS is live; system/alarm providers will be connected in later branches.

## Goals for this phase

- Route server TTS audio through one state machine.
- Flush stale audio immediately on interruption.
- Wait for actual playback drain before switching back to listening.
- Preserve the existing Opus/audio codec pipeline.
- Keep provider selection extensible without rewriting `Application` again.

## CI validation

This branch is validated through the repository ESP-IDF 5.4 / ESP32-S3 pull-request build before it is merged to `main`.

## Next TTS steps

After this branch is merged, later speech work can add a provider bridge on RDK X5 or another local host, streaming PCM/Opus back to this same engine. A dedicated MOSS voice, local fallback prompts, sentence-level streaming and explicit system/alarm preemption can then be added without changing the basic device audio lifecycle.
