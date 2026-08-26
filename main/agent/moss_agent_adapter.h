#pragma once

#include <cstdint>
#include <mutex>
#include <string>

#include <cJSON.h>
#include <esp_log.h>

#include "settings.h"

namespace moss::agent {

enum class AgentBackend : uint8_t {
    Xiaozhi = 0,
    MossGateway = 1,
};

enum class AgentPhase : uint8_t {
    Offline = 0,
    Idle = 1,
    Listening = 2,
    Thinking = 3,
    Speaking = 4,
    ExecutingTool = 5,
    Error = 6,
};

struct AgentSnapshot {
    AgentBackend backend = AgentBackend::Xiaozhi;
    AgentPhase phase = AgentPhase::Offline;
    std::string session_id;
    std::string last_user_text;
    std::string last_assistant_text;
    uint32_t event_sequence = 0;
};

// Device-side adapter between the ESP32 runtime and an AI backend.
// The ESP32 does not run the LLM. It owns a stable backend identity, session
// state and event contract so Xiaozhi can remain the default while a RDK X5 or
// self-hosted MOSS Gateway can be connected later without rewriting hardware.
class MossAgentAdapter {
public:
    static MossAgentAdapter& GetInstance() {
        static MossAgentAdapter instance;
        return instance;
    }

    void Load() {
        Settings settings("moss_agent");
        const auto value = settings.GetString("backend", "xiaozhi");
        std::lock_guard<std::mutex> lock(mutex_);
        state_.backend = ParseBackend(value);
        ESP_LOGI("MossAgentAdapter", "backend=%s", BackendName(state_.backend));
    }

    AgentBackend backend() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return state_.backend;
    }

    bool SetBackend(AgentBackend backend, bool persist = true) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            state_.backend = backend;
            state_.event_sequence++;
        }
        if (persist) {
            Settings settings("moss_agent", true);
            settings.SetString("backend", BackendName(backend));
        }
        return true;
    }

    bool SetBackend(const std::string& backend, bool persist = true) {
        if (backend != "xiaozhi" && backend != "moss-gateway" &&
            backend != "moss" && backend != "gateway") {
            return false;
        }
        return SetBackend(ParseBackend(backend), persist);
    }

    AgentSnapshot snapshot() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return state_;
    }

    void OnChannelOpened(const std::string& session_id) {
        std::lock_guard<std::mutex> lock(mutex_);
        state_.session_id = session_id;
        SetPhaseLocked(AgentPhase::Idle);
    }

    void OnChannelClosed() {
        std::lock_guard<std::mutex> lock(mutex_);
        state_.session_id.clear();
        SetPhaseLocked(AgentPhase::Offline);
    }

    void OnListening() {
        std::lock_guard<std::mutex> lock(mutex_);
        SetPhaseLocked(AgentPhase::Listening);
    }

    void OnThinking() {
        std::lock_guard<std::mutex> lock(mutex_);
        SetPhaseLocked(AgentPhase::Thinking);
    }

    void OnUserText(const std::string& text) {
        std::lock_guard<std::mutex> lock(mutex_);
        state_.last_user_text = text;
        SetPhaseLocked(AgentPhase::Thinking);
    }

    void OnAssistantText(const std::string& text) {
        std::lock_guard<std::mutex> lock(mutex_);
        state_.last_assistant_text = text;
        state_.event_sequence++;
    }

    void OnSpeaking(bool speaking) {
        std::lock_guard<std::mutex> lock(mutex_);
        SetPhaseLocked(speaking ? AgentPhase::Speaking : AgentPhase::Idle);
    }

    void OnToolState(bool executing) {
        std::lock_guard<std::mutex> lock(mutex_);
        SetPhaseLocked(executing ? AgentPhase::ExecutingTool : AgentPhase::Thinking);
    }

    void OnError() {
        std::lock_guard<std::mutex> lock(mutex_);
        SetPhaseLocked(AgentPhase::Error);
    }

    std::string BuildHello(const std::string& board_type,
                           const std::string& board_name,
                           bool has_mcp,
                           bool has_iot,
                           bool has_audio) const {
        const auto current = snapshot();
        cJSON* root = cJSON_CreateObject();
        cJSON_AddStringToObject(root, "event", "hello");
        cJSON_AddStringToObject(root, "protocol", "moss-agent/1.0");
        cJSON_AddStringToObject(root, "backend", BackendName(current.backend));
        cJSON_AddStringToObject(root, "board_type", board_type.c_str());
        cJSON_AddStringToObject(root, "board_name", board_name.c_str());
        cJSON_AddStringToObject(root, "session_id", current.session_id.c_str());
        cJSON* caps = cJSON_AddObjectToObject(root, "capabilities");
        cJSON_AddBoolToObject(caps, "audio", has_audio);
        cJSON_AddBoolToObject(caps, "mcp", has_mcp);
        cJSON_AddBoolToObject(caps, "iot", has_iot);
        cJSON_AddBoolToObject(caps, "tts_stream", true);
        cJSON_AddBoolToObject(caps, "barge_in", true);
        cJSON_AddBoolToObject(caps, "memory", true);
        cJSON_AddStringToObject(caps, "memory_scope", "device-local");
        cJSON_AddBoolToObject(caps, "hardware_profile", true);
        auto result = PrintJson(root);
        cJSON_Delete(root);
        return result;
    }

    std::string BuildStateEvent(const char* reason = nullptr) {
        AgentSnapshot current;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            state_.event_sequence++;
            current = state_;
        }
        cJSON* root = cJSON_CreateObject();
        cJSON_AddStringToObject(root, "event", "state");
        cJSON_AddNumberToObject(root, "seq", current.event_sequence);
        cJSON_AddStringToObject(root, "backend", BackendName(current.backend));
        cJSON_AddStringToObject(root, "phase", PhaseName(current.phase));
        cJSON_AddStringToObject(root, "session_id", current.session_id.c_str());
        if (!current.last_user_text.empty()) {
            cJSON_AddStringToObject(root, "last_user_text", current.last_user_text.c_str());
        }
        if (!current.last_assistant_text.empty()) {
            cJSON_AddStringToObject(root, "last_assistant_text", current.last_assistant_text.c_str());
        }
        if (reason && reason[0] != '\0') {
            cJSON_AddStringToObject(root, "reason", reason);
        }
        auto result = PrintJson(root);
        cJSON_Delete(root);
        return result;
    }

    static const char* BackendName(AgentBackend backend) {
        switch (backend) {
            case AgentBackend::Xiaozhi: return "xiaozhi";
            case AgentBackend::MossGateway: return "moss-gateway";
            default: return "unknown";
        }
    }

    static const char* PhaseName(AgentPhase phase) {
        switch (phase) {
            case AgentPhase::Offline: return "offline";
            case AgentPhase::Idle: return "idle";
            case AgentPhase::Listening: return "listening";
            case AgentPhase::Thinking: return "thinking";
            case AgentPhase::Speaking: return "speaking";
            case AgentPhase::ExecutingTool: return "executing_tool";
            case AgentPhase::Error: return "error";
            default: return "unknown";
        }
    }

private:
    MossAgentAdapter() = default;
    MossAgentAdapter(const MossAgentAdapter&) = delete;
    MossAgentAdapter& operator=(const MossAgentAdapter&) = delete;

    static AgentBackend ParseBackend(const std::string& value) {
        if (value == "moss-gateway" || value == "moss" || value == "gateway") {
            return AgentBackend::MossGateway;
        }
        return AgentBackend::Xiaozhi;
    }

    static std::string PrintJson(cJSON* root) {
        char* raw = cJSON_PrintUnformatted(root);
        std::string result = raw ? raw : "{}";
        if (raw) {
            cJSON_free(raw);
        }
        return result;
    }

    void SetPhaseLocked(AgentPhase phase) {
        if (state_.phase != phase) {
            state_.phase = phase;
            state_.event_sequence++;
        }
    }

    mutable std::mutex mutex_;
    AgentSnapshot state_;
};

}  // namespace moss::agent
