#pragma once

#include <cstdint>
#include <functional>
#include <mutex>
#include <string>
#include <utility>

#include "protocols/protocol.h"

namespace moss::speech {

enum class TtsSource : uint8_t {
    XiaozhiServer = 0,
    ExternalStream = 1,
    LocalAsset = 2,
};

enum class TtsPriority : uint8_t {
    Chat = 10,
    System = 50,
    Alarm = 100,
};

enum class TtsState : uint8_t {
    Idle = 0,
    Streaming = 1,
    Draining = 2,
    Interrupted = 3,
};

struct TtsSession {
    TtsSource source = TtsSource::XiaozhiServer;
    TtsPriority priority = TtsPriority::Chat;
    std::string id;
    std::string text;
};

// TtsEngine is intentionally synthesis-provider agnostic.  The ESP32 side owns
// stream admission, priority/preemption and playback lifecycle, while the
// actual speech bytes may come from Xiaozhi today and a local/cloud provider
// later.  This keeps the audio pipeline stable when providers are changed.
class TtsEngine {
public:
    using PacketSink = std::function<bool(AudioStreamPacket&& packet)>;
    using StateCallback = std::function<void(TtsState state, const TtsSession& session)>;

    void SetPacketSink(PacketSink sink) {
        std::lock_guard<std::mutex> lock(mutex_);
        packet_sink_ = std::move(sink);
    }

    void SetStateCallback(StateCallback callback) {
        std::lock_guard<std::mutex> lock(mutex_);
        state_callback_ = std::move(callback);
    }

    // Starts a new utterance.  An utterance with lower priority cannot replace
    // an active higher-priority utterance. Equal or higher priority may replace
    // the active session so future Alarm/System providers can preempt Chat.
    bool Begin(TtsSource source, TtsPriority priority, std::string id = {}) {
        StateCallback callback;
        TtsSession interrupted_session;
        bool notify_interrupted = false;
        TtsSession started_session;

        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (IsActiveLocked() && static_cast<uint8_t>(priority) < static_cast<uint8_t>(session_.priority)) {
                return false;
            }

            if (IsActiveLocked()) {
                interrupted_session = session_;
                notify_interrupted = true;
            }

            session_ = TtsSession{source, priority, std::move(id), {}};
            state_ = TtsState::Streaming;
            started_session = session_;
            callback = state_callback_;
        }

        if (callback && notify_interrupted) {
            callback(TtsState::Interrupted, interrupted_session);
        }
        if (callback) {
            callback(TtsState::Streaming, started_session);
        }
        return true;
    }

    bool PushAudio(AudioStreamPacket&& packet) {
        PacketSink sink;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (state_ != TtsState::Streaming || !packet_sink_) {
                return false;
            }
            sink = packet_sink_;
        }
        return sink(std::move(packet));
    }

    void SetCurrentText(std::string text) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (state_ == TtsState::Streaming || state_ == TtsState::Draining) {
            session_.text = std::move(text);
        }
    }

    // Called when the provider has finished sending packets. Playback may still
    // have buffered Opus frames, so the engine enters Draining rather than Idle.
    // Interrupted streams also enter Draining when the provider acknowledges
    // stop; their audio queue should already have been flushed by Application.
    void EndInput() {
        StateCallback callback;
        TtsSession session;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (state_ != TtsState::Streaming && state_ != TtsState::Interrupted) {
                return;
            }
            state_ = TtsState::Draining;
            callback = state_callback_;
            session = session_;
        }
        if (callback) {
            callback(TtsState::Draining, session);
        }
    }

    // Called by the audio loop only after the decode queue and decoder worker are
    // both empty. This is the point at which it is safe to return to listening.
    bool MarkPlaybackDrained() {
        StateCallback callback;
        TtsSession session;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (state_ != TtsState::Draining) {
                return false;
            }
            state_ = TtsState::Idle;
            callback = state_callback_;
            session = session_;
        }
        if (callback) {
            callback(TtsState::Idle, session);
        }
        return true;
    }

    bool Interrupt(TtsPriority requester = TtsPriority::Alarm, bool force = true) {
        StateCallback callback;
        TtsSession session;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (!IsActiveLocked()) {
                return false;
            }
            if (!force && static_cast<uint8_t>(requester) < static_cast<uint8_t>(session_.priority)) {
                return false;
            }
            state_ = TtsState::Interrupted;
            callback = state_callback_;
            session = session_;
        }
        if (callback) {
            callback(TtsState::Interrupted, session);
        }
        return true;
    }

    void Reset() {
        StateCallback callback;
        TtsSession previous;
        bool changed = false;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            changed = state_ != TtsState::Idle;
            previous = session_;
            state_ = TtsState::Idle;
            session_ = {};
            callback = state_callback_;
        }
        if (callback && changed) {
            callback(TtsState::Idle, previous);
        }
    }

    bool IsActive() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return IsActiveLocked();
    }

    bool IsDraining() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return state_ == TtsState::Draining;
    }

    TtsState state() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return state_;
    }

    TtsSession session() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return session_;
    }

    static const char* StateName(TtsState state) {
        switch (state) {
            case TtsState::Idle: return "idle";
            case TtsState::Streaming: return "streaming";
            case TtsState::Draining: return "draining";
            case TtsState::Interrupted: return "interrupted";
            default: return "unknown";
        }
    }

    static const char* SourceName(TtsSource source) {
        switch (source) {
            case TtsSource::XiaozhiServer: return "xiaozhi-server";
            case TtsSource::ExternalStream: return "external-stream";
            case TtsSource::LocalAsset: return "local-asset";
            default: return "unknown";
        }
    }

private:
    bool IsActiveLocked() const {
        return state_ == TtsState::Streaming || state_ == TtsState::Draining;
    }

    mutable std::mutex mutex_;
    TtsState state_ = TtsState::Idle;
    TtsSession session_;
    PacketSink packet_sink_;
    StateCallback state_callback_;
};

}  // namespace moss::speech
