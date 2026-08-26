#include "protocol.h"

#include <esp_log.h>

#include "agent/moss_agent_adapter.h"

#define TAG "Protocol"

namespace {

void ObserveAgentJson(const cJSON* root) {
    if (!root || !cJSON_IsObject(root)) {
        return;
    }

    const cJSON* type = cJSON_GetObjectItem(root, "type");
    if (!cJSON_IsString(type)) {
        return;
    }

    auto& agent = moss::agent::MossAgentAdapter::GetInstance();
    const std::string type_value = type->valuestring;

    if (type_value == "stt") {
        const cJSON* text = cJSON_GetObjectItem(root, "text");
        if (cJSON_IsString(text)) {
            agent.OnUserText(text->valuestring);
        } else {
            agent.OnThinking();
        }
        return;
    }

    if (type_value == "tts") {
        const cJSON* state = cJSON_GetObjectItem(root, "state");
        if (!cJSON_IsString(state)) {
            return;
        }

        const std::string state_value = state->valuestring;
        if (state_value == "start") {
            agent.OnSpeaking(true);
        } else if (state_value == "sentence_start") {
            const cJSON* text = cJSON_GetObjectItem(root, "text");
            if (cJSON_IsString(text)) {
                agent.OnAssistantText(text->valuestring);
            }
        }
        // Do not mark speaking=false on provider "stop". The Application keeps
        // draining buffered Opus after that message. Runtime reconciliation will
        // move the phase back to listening/idle only after playback really ends.
    }
}

}  // namespace

void Protocol::OnIncomingJson(std::function<void(const cJSON* root)> callback) {
    on_incoming_json_ = [callback = std::move(callback)](const cJSON* root) {
        ObserveAgentJson(root);
        if (callback) {
            callback(root);
        }
    };
}

void Protocol::OnIncomingAudio(std::function<void(AudioStreamPacket&& packet)> callback) {
    on_incoming_audio_ = callback;
}

void Protocol::OnAudioChannelOpened(std::function<void()> callback) {
    on_audio_channel_opened_ = [this, callback = std::move(callback)]() {
        moss::agent::MossAgentAdapter::GetInstance().OnChannelOpened(session_id_);
        if (callback) {
            callback();
        }
    };
}

void Protocol::OnAudioChannelClosed(std::function<void()> callback) {
    on_audio_channel_closed_ = [callback = std::move(callback)]() {
        moss::agent::MossAgentAdapter::GetInstance().OnChannelClosed();
        if (callback) {
            callback();
        }
    };
}

void Protocol::OnNetworkError(std::function<void(const std::string& message)> callback) {
    on_network_error_ = [callback = std::move(callback)](const std::string& message) {
        moss::agent::MossAgentAdapter::GetInstance().OnError();
        if (callback) {
            callback(message);
        }
    };
}

void Protocol::SetError(const std::string& message) {
    error_occurred_ = true;
    if (on_network_error_ != nullptr) {
        on_network_error_(message);
    } else {
        moss::agent::MossAgentAdapter::GetInstance().OnError();
    }
}

void Protocol::SendAbortSpeaking(AbortReason reason) {
    std::string message = "{\"session_id\":\"" + session_id_ + "\",\"type\":\"abort\"";
    if (reason == kAbortReasonWakeWordDetected) {
        message += ",\"reason\":\"wake_word_detected\"";
    }
    message += "}";
    SendText(message);
}

void Protocol::SendWakeWordDetected(const std::string& wake_word) {
    std::string json = "{\"session_id\":\"" + session_id_ + 
                      "\",\"type\":\"listen\",\"state\":\"detect\",\"text\":\"" + wake_word + "\"}";
    SendText(json);
}

void Protocol::SendStartListening(ListeningMode mode) {
    moss::agent::MossAgentAdapter::GetInstance().OnListening();

    std::string message = "{\"session_id\":\"" + session_id_ + "\"";
    message += ",\"type\":\"listen\",\"state\":\"start\"";
    if (mode == kListeningModeRealtime) {
        message += ",\"mode\":\"realtime\"";
    } else if (mode == kListeningModeAutoStop) {
        message += ",\"mode\":\"auto\"";
    } else {
        message += ",\"mode\":\"manual\"";
    }
    message += "}";
    SendText(message);
}

void Protocol::SendStopListening() {
    moss::agent::MossAgentAdapter::GetInstance().OnThinking();

    std::string message = "{\"session_id\":\"" + session_id_ + "\",\"type\":\"listen\",\"state\":\"stop\"}";
    SendText(message);
}

void Protocol::SendIotDescriptors(const std::string& descriptors) {
    cJSON* root = cJSON_Parse(descriptors.c_str());
    if (root == nullptr) {
        ESP_LOGE(TAG, "Failed to parse IoT descriptors: %s", descriptors.c_str());
        return;
    }

    if (!cJSON_IsArray(root)) {
        ESP_LOGE(TAG, "IoT descriptors should be an array");
        cJSON_Delete(root);
        return;
    }

    int arraySize = cJSON_GetArraySize(root);
    for (int i = 0; i < arraySize; ++i) {
        cJSON* descriptor = cJSON_GetArrayItem(root, i);
        if (descriptor == nullptr) {
            ESP_LOGE(TAG, "Failed to get IoT descriptor at index %d", i);
            continue;
        }

        cJSON* messageRoot = cJSON_CreateObject();
        cJSON_AddStringToObject(messageRoot, "session_id", session_id_.c_str());
        cJSON_AddStringToObject(messageRoot, "type", "iot");
        cJSON_AddBoolToObject(messageRoot, "update", true);

        cJSON* descriptorArray = cJSON_CreateArray();
        cJSON_AddItemToArray(descriptorArray, cJSON_Duplicate(descriptor, 1));
        cJSON_AddItemToObject(messageRoot, "descriptors", descriptorArray);

        char* message = cJSON_PrintUnformatted(messageRoot);
        if (message == nullptr) {
            ESP_LOGE(TAG, "Failed to print JSON message for IoT descriptor at index %d", i);
            cJSON_Delete(messageRoot);
            continue;
        }

        SendText(std::string(message));
        cJSON_free(message);
        cJSON_Delete(messageRoot);
    }

    cJSON_Delete(root);
}

void Protocol::SendIotStates(const std::string& states) {
    std::string message = "{\"session_id\":\"" + session_id_ + "\",\"type\":\"iot\",\"update\":true,\"states\":" + states + "}";
    SendText(message);
}

void Protocol::SendMcpMessage(const std::string& payload) {
    std::string message = "{\"session_id\":\"" + session_id_ + "\",\"type\":\"mcp\",\"payload\":" + payload + "}";
    SendText(message);
}

bool Protocol::IsTimeout() const {
    const int kTimeoutSeconds = 120;
    auto now = std::chrono::steady_clock::now();
    auto duration = std::chrono::duration_cast<std::chrono::seconds>(now - last_incoming_time_);
    bool timeout = duration.count() > kTimeoutSeconds;
    if (timeout) {
        ESP_LOGE(TAG, "Channel timeout %ld seconds", (long)duration.count());
    }
    return timeout;
}
