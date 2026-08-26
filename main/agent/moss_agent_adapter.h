#pragma once

#include <cstdint>
#include <mutex>
#include <string>

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
//
// The default backend remains Xiaozhi for compatibility. MossGateway mode does
// not run an LLM on the ESP32; instead it enables a stable event/capability
// contract that a RDK X5, local server or future MOSS Core can consume.
class MossAgentAdapter {
public:
    static MossAgentAdapter& GetInstance();

    void Load();

    AgentBackend backend() const;
    bool SetBackend(AgentBackend backend, bool persist = true);
    bool SetBackend(const std::string& backend, bool persist = true);

    AgentSnapshot snapshot() const;

    void OnChannelOpened(const std::string& session_id);
    void OnChannelClosed();
    void OnListening();
    void OnThinking();
    void OnUserText(const std::string& text);
    void OnAssistantText(const std::string& text);
    void OnSpeaking(bool speaking);
    void OnToolState(bool executing);
    void OnError();

    // JSON object strings used as the payload of Protocol::SendAgentMessage().
    std::string BuildHello(const std::string& board_type,
                           const std::string& board_name,
                           bool has_mcp,
                           bool has_iot,
                           bool has_audio) const;
    std::string BuildStateEvent(const char* reason = nullptr);

    static const char* BackendName(AgentBackend backend);
    static const char* PhaseName(AgentPhase phase);

private:
    MossAgentAdapter() = default;
    MossAgentAdapter(const MossAgentAdapter&) = delete;
    MossAgentAdapter& operator=(const MossAgentAdapter&) = delete;

    void SetPhaseLocked(AgentPhase phase);

    mutable std::mutex mutex_;
    AgentSnapshot state_;
};

}  // namespace moss::agent
