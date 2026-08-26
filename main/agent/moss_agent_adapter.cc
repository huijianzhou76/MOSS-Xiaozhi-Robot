#include "agent/moss_agent_adapter.h"

#include <cJSON.h>
#include <esp_log.h>

#include "settings.h"

#define TAG "MossAgentAdapter"

namespace moss::agent {
namespace {

std::string PrintJson(cJSON* root) {
    char* raw = cJSON_PrintUnformatted(root);
    std::string result = raw ? raw : "{}";
    if (raw) {
        cJSON_free(raw);
    }
    return result;
}

AgentBackend ParseBackend(const std::string& value) {
    if (value == "moss-gateway" || value == "moss" || value == "gateway") {
        return AgentBackend::MossGateway;
    }
    return AgentBackend::Xiaozhi;
}

}  // namespace

MossAgentAdapter& MossAgentAdapter::GetInstance() {
    static MossAgentAdapter instance;
    return instance;
}

void MossAgentAdapter::Load() {
    Settings settings("moss_agent");
    const auto value = settings.GetString("backend", "xiaozhi");
    std::lock_guard<std::mutex> lock(mutex_);
    state_.backend = ParseBackend(value);
    ESP_LOGI(TAG, "backend=%s", BackendName(state_.backend));
}

AgentBackend MossAgentAdapter::backend() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return state_.backend;
}

bool MossAgentAdapter::SetBackend(AgentBackend backend, bool persist) {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        state_.backend = backend;
        state_.event_sequence++;
    }

    if (persist) {
        Settings settings("moss_agent", true);
        settings.SetString("backend", BackendName(backend));
    }
    ESP_LOGI(TAG, "backend changed to %s", BackendName(backend));
    return true;
}

bool MossAgentAdapter::SetBackend(const std::string& backend, bool persist) {
    if (backend != "xiaozhi" && backend != "moss-gateway" &&
        backend != "moss" && backend != "gateway") {
        return false;
    }
    return SetBackend(ParseBackend(backend), persist);
}

AgentSnapshot MossAgentAdapter::snapshot() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return state_;
}

void MossAgentAdapter::SetPhaseLocked(AgentPhase phase) {
    if (state_.phase != phase) {
        state_.phase = phase;
        state_.event_sequence++;
    }
}

void MossAgentAdapter::OnChannelOpened(const std::string& session_id) {
    std::lock_guard<std::mutex> lock(mutex_);
    state_.session_id = session_id;
    SetPhaseLocked(AgentPhase::Idle);
}

void MossAgentAdapter::OnChannelClosed() {
    std::lock_guard<std::mutex> lock(mutex_);
    state_.session_id.clear();
    SetPhaseLocked(AgentPhase::Offline);
}

void MossAgentAdapter::OnListening() {
    std::lock_guard<std::mutex> lock(mutex_);
    SetPhaseLocked(AgentPhase::Listening);
}

void MossAgentAdapter::OnThinking() {
    std::lock_guard<std::mutex> lock(mutex_);
    SetPhaseLocked(AgentPhase::Thinking);
}

void MossAgentAdapter::OnUserText(const std::string& text) {
    std::lock_guard<std::mutex> lock(mutex_);
    state_.last_user_text = text;
    SetPhaseLocked(AgentPhase::Thinking);
}

void MossAgentAdapter::OnAssistantText(const std::string& text) {
    std::lock_guard<std::mutex> lock(mutex_);
    state_.last_assistant_text = text;
    state_.event_sequence++;
}

void MossAgentAdapter::OnSpeaking(bool speaking) {
    std::lock_guard<std::mutex> lock(mutex_);
    SetPhaseLocked(speaking ? AgentPhase::Speaking : AgentPhase::Idle);
}

void MossAgentAdapter::OnToolState(bool executing) {
    std::lock_guard<std::mutex> lock(mutex_);
    SetPhaseLocked(executing ? AgentPhase::ExecutingTool : AgentPhase::Thinking);
}

void MossAgentAdapter::OnError() {
    std::lock_guard<std::mutex> lock(mutex_);
    SetPhaseLocked(AgentPhase::Error);
}

std::string MossAgentAdapter::BuildHello(const std::string& board_type,
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

    auto result = PrintJson(root);
    cJSON_Delete(root);
    return result;
}

std::string MossAgentAdapter::BuildStateEvent(const char* reason) {
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

const char* MossAgentAdapter::BackendName(AgentBackend backend) {
    switch (backend) {
        case AgentBackend::Xiaozhi: return "xiaozhi";
        case AgentBackend::MossGateway: return "moss-gateway";
        default: return "unknown";
    }
}

const char* MossAgentAdapter::PhaseName(AgentPhase phase) {
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

}  // namespace moss::agent
