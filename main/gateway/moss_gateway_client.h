#pragma once

#include <algorithm>
#include <mutex>
#include <string>

#include <cJSON.h>
#include <esp_log.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <web_socket.h>

#include "agent/moss_agent_adapter.h"
#include "board.h"
#include "settings.h"
#include "moss_gateway_read_bridge.h"

namespace moss::gateway {

class MossGatewayClient {
public:
    static constexpr int kHeartbeatIntervalSeconds = 15;
    static constexpr int kWelcomeTimeoutSeconds = 10;
    static constexpr int kInitialReconnectSeconds = 1;
    static constexpr int kMaxReconnectSeconds = 60;

    static MossGatewayClient& GetInstance() {
        static MossGatewayClient instance;
        return instance;
    }

    void Load() {
        Settings settings("moss_gateway");
        std::lock_guard<std::mutex> lock(mutex_);
        url_ = settings.GetString("url", "");
        token_ = settings.GetString("token", "");
        loaded_ = true;
    }

    bool Configure(const std::string& url,
                   const std::string& token,
                   std::string* error = nullptr) {
        if (!IsValidUrl(url)) {
            SetError(error, "gateway URL must use ws:// or wss:// and must not embed credentials");
            return false;
        }
        if (url.size() > 256) {
            SetError(error, "gateway URL is too long");
            return false;
        }
        if (token.empty() || token.size() > 256) {
            SetError(error, "device token must be 1-256 bytes");
            return false;
        }

        Settings settings("moss_gateway", true);
        settings.SetString("url", url);
        settings.SetString("token", token);

        std::lock_guard<std::mutex> lock(mutex_);
        url_ = url;
        token_ = token;
        loaded_ = true;
        return true;
    }

    void NotifyNetworkReady() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            network_ready_ = true;
        }
        moss::agent::MossAgentAdapter::GetInstance().Load();
        EnsureAutonomyTask();
    }

    void NotifyNetworkUnavailable() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            network_ready_ = false;
        }
        std::lock_guard<std::recursive_mutex> operation_lock(operation_mutex_);
        DisconnectSocket();
    }

    bool Start(std::string* error = nullptr) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            manual_suspended_ = false;
        }
        EnsureAutonomyTask();
        return Connect(error);
    }

    void Stop() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            manual_suspended_ = true;
        }
        std::lock_guard<std::recursive_mutex> operation_lock(operation_mutex_);
        DisconnectSocket();
    }

    bool SendHeartbeat(std::string* error = nullptr) {
        cJSON* root = cJSON_CreateObject();
        cJSON_AddStringToObject(root, "event", "heartbeat");
        cJSON_AddStringToObject(root, "phase",
            moss::agent::MossAgentAdapter::PhaseName(
                moss::agent::MossAgentAdapter::GetInstance().snapshot().phase));
        const bool ok = SendJson(root, error);
        cJSON_Delete(root);
        return ok;
    }

    bool SendState(std::string* error = nullptr) {
        auto& adapter = moss::agent::MossAgentAdapter::GetInstance();
        const std::string state = adapter.BuildStateEvent("gateway_sync");
        return SendRaw(state, error);
    }

    std::string StatusJson() {
        EnsureLoaded();
        std::lock_guard<std::mutex> lock(mutex_);
        cJSON* root = cJSON_CreateObject();
        cJSON_AddStringToObject(root, "schema", "moss-gateway-client/1.2");
        cJSON_AddBoolToObject(root, "configured", ConfiguredLocked());
        cJSON_AddBoolToObject(root, "connected", connected_);
        cJSON_AddBoolToObject(root, "welcomed", welcomed_);
        cJSON_AddBoolToObject(root, "connecting", connecting_);
        cJSON_AddBoolToObject(root, "network_ready", network_ready_);
        cJSON_AddBoolToObject(root, "autonomy_started", autonomy_task_ != nullptr);
        cJSON_AddBoolToObject(root, "manual_suspended", manual_suspended_);
        cJSON_AddBoolToObject(root, "gateway_tool_bridge", true);
        cJSON_AddStringToObject(root, "gateway_tool_bridge_mode", "read-only");
        const std::string safe_url = RedactUrl(url_);
        cJSON_AddStringToObject(root, "url", safe_url.c_str());
        cJSON_AddBoolToObject(root, "url_query_redacted", safe_url != url_);
        cJSON_AddBoolToObject(root, "token_configured", !token_.empty());
        cJSON_AddBoolToObject(root, "token_exposed", false);
        cJSON_AddNumberToObject(root, "messages_sent", messages_sent_);
        cJSON_AddNumberToObject(root, "messages_received", messages_received_);
        cJSON_AddNumberToObject(root, "reconnect_attempts", reconnect_attempts_);
        cJSON_AddNumberToObject(root, "reconnect_failures", reconnect_failures_);
        cJSON_AddNumberToObject(root, "heartbeat_failures", heartbeat_failures_);
        cJSON_AddNumberToObject(root, "current_backoff_seconds", current_backoff_seconds_);
        cJSON_AddNumberToObject(root, "heartbeat_interval_seconds", kHeartbeatIntervalSeconds);
        cJSON_AddNumberToObject(root, "max_reconnect_seconds", kMaxReconnectSeconds);
        if (!gateway_session_id_.empty()) {
            cJSON_AddStringToObject(root, "gateway_session_id", gateway_session_id_.c_str());
        }
        if (!last_event_.empty()) {
            cJSON_AddStringToObject(root, "last_event", last_event_.c_str());
        }
        if (!last_error_.empty()) {
            cJSON_AddStringToObject(root, "last_error", last_error_.c_str());
        }
        const std::string result = PrintJson(root);
        cJSON_Delete(root);
        return result;
    }

private:
    MossGatewayClient() = default;
    MossGatewayClient(const MossGatewayClient&) = delete;
    MossGatewayClient& operator=(const MossGatewayClient&) = delete;

    void EnsureLoaded() {
        bool should_load = false;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            should_load = !loaded_;
        }
        if (should_load) {
            Load();
        }
    }

    void EnsureAutonomyTask() {
        std::lock_guard<std::mutex> lock(mutex_);
        if (autonomy_task_ != nullptr) {
            return;
        }

        TaskHandle_t handle = nullptr;
        const BaseType_t created = xTaskCreate(
            [](void* arg) {
                static_cast<MossGatewayClient*>(arg)->AutonomyLoop();
                vTaskDelete(nullptr);
            },
            "moss_gateway",
            4096,
            this,
            2,
            &handle);
        if (created != pdPASS) {
            last_error_ = "failed to create gateway autonomy task";
            ESP_LOGE("MossGatewayClient", "%s", last_error_.c_str());
            return;
        }
        autonomy_task_ = handle;
    }

    void AutonomyLoop() {
        int reconnect_delay = kInitialReconnectSeconds;
        int heartbeat_elapsed = 0;
        int welcome_elapsed = 0;

        while (true) {
            EnsureLoaded();

            bool network_ready = false;
            bool manual_suspended = false;
            bool connected = false;
            bool welcomed = false;
            bool configured = false;
            {
                std::lock_guard<std::mutex> lock(mutex_);
                network_ready = network_ready_;
                manual_suspended = manual_suspended_;
                connected = connected_;
                welcomed = welcomed_;
                configured = ConfiguredLocked();
            }

            auto& adapter = moss::agent::MossAgentAdapter::GetInstance();
            const bool gateway_backend =
                adapter.backend() == moss::agent::AgentBackend::MossGateway;

            if (!network_ready || manual_suspended || !configured || !gateway_backend) {
                if (connected && (!network_ready || !gateway_backend)) {
                    std::lock_guard<std::recursive_mutex> operation_lock(operation_mutex_);
                    DisconnectSocket();
                }
                reconnect_delay = kInitialReconnectSeconds;
                heartbeat_elapsed = 0;
                welcome_elapsed = 0;
                {
                    std::lock_guard<std::mutex> lock(mutex_);
                    current_backoff_seconds_ = reconnect_delay;
                }
                vTaskDelay(pdMS_TO_TICKS(1000));
                continue;
            }

            if (!connected) {
                {
                    std::lock_guard<std::mutex> lock(mutex_);
                    ++reconnect_attempts_;
                    current_backoff_seconds_ = reconnect_delay;
                }

                std::string error;
                if (Connect(&error)) {
                    reconnect_delay = kInitialReconnectSeconds;
                    heartbeat_elapsed = 0;
                    welcome_elapsed = 0;
                    {
                        std::lock_guard<std::mutex> lock(mutex_);
                        current_backoff_seconds_ = reconnect_delay;
                    }
                    vTaskDelay(pdMS_TO_TICKS(1000));
                    continue;
                }

                {
                    std::lock_guard<std::mutex> lock(mutex_);
                    ++reconnect_failures_;
                    if (!error.empty()) {
                        last_error_ = error;
                    }
                }
                vTaskDelay(pdMS_TO_TICKS(reconnect_delay * 1000));
                reconnect_delay = std::min(reconnect_delay * 2, kMaxReconnectSeconds);
                continue;
            }

            if (!welcomed) {
                ++welcome_elapsed;
                if (welcome_elapsed >= kWelcomeTimeoutSeconds) {
                    {
                        std::lock_guard<std::mutex> lock(mutex_);
                        last_error_ = "gateway welcome timeout";
                    }
                    std::lock_guard<std::recursive_mutex> operation_lock(operation_mutex_);
                    DisconnectSocket();
                    welcome_elapsed = 0;
                }
                vTaskDelay(pdMS_TO_TICKS(1000));
                continue;
            }

            welcome_elapsed = 0;
            bool needs_state_sync = false;
            {
                std::lock_guard<std::mutex> lock(mutex_);
                needs_state_sync = !initial_state_synced_;
            }
            if (needs_state_sync) {
                std::string error;
                if (SendState(&error)) {
                    std::lock_guard<std::mutex> lock(mutex_);
                    initial_state_synced_ = true;
                } else {
                    std::lock_guard<std::mutex> lock(mutex_);
                    last_error_ = error.empty() ? "initial state sync failed" : error;
                }
            }

            ++heartbeat_elapsed;
            if (heartbeat_elapsed >= kHeartbeatIntervalSeconds) {
                heartbeat_elapsed = 0;
                std::string error;
                if (!SendHeartbeat(&error)) {
                    {
                        std::lock_guard<std::mutex> lock(mutex_);
                        ++heartbeat_failures_;
                        last_error_ = error.empty() ? "gateway heartbeat failed" : error;
                    }
                    std::lock_guard<std::recursive_mutex> operation_lock(operation_mutex_);
                    DisconnectSocket();
                }
            }

            vTaskDelay(pdMS_TO_TICKS(1000));
        }
    }

    bool Connect(std::string* error) {
        std::lock_guard<std::recursive_mutex> operation_lock(operation_mutex_);
        EnsureLoaded();

        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (websocket_ && websocket_->IsConnected()) {
                connected_ = true;
                return true;
            }
            if (connecting_) {
                SetError(error, "gateway connection is already in progress");
                return false;
            }
            if (url_.empty()) {
                SetError(error, "gateway URL is not configured");
                return false;
            }
            if (token_.empty()) {
                SetError(error, "gateway device token is not configured");
                return false;
            }
            connecting_ = true;
        }

        auto finish_connect = [this]() {
            std::lock_guard<std::mutex> lock(mutex_);
            connecting_ = false;
        };

        auto& adapter = moss::agent::MossAgentAdapter::GetInstance();
        adapter.Load();
        if (adapter.backend() != moss::agent::AgentBackend::MossGateway) {
            finish_connect();
            SetError(error, "agent backend must be moss-gateway before connecting");
            return false;
        }

        std::string url;
        std::string token;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            url = url_;
            token = token_;
            last_error_.clear();
            gateway_session_id_.clear();
            initial_state_synced_ = false;
        }

        DisconnectSocket();

        WebSocket* socket = Board::GetInstance().CreateWebSocketForUrl(url);
        if (!socket) {
            finish_connect();
            SetError(error, "failed to create websocket client");
            return false;
        }

        const std::string authorization = "Bearer " + token;
        socket->SetHeader("Authorization", authorization.c_str());
        socket->SetHeader("X-MOSS-Device-Token", token.c_str());
        socket->SetHeader("MOSS-Protocol", "moss-agent/1.0");
        socket->SetHeader("Device-Id", Board::GetInstance().GetUuid().c_str());

        socket->OnData([this](const char* data, size_t len, bool binary) {
            if (binary || !data || len == 0) {
                return;
            }
            OnText(data, len);
        });

        socket->OnDisconnected([this]() {
            std::lock_guard<std::mutex> lock(mutex_);
            connected_ = false;
            welcomed_ = false;
            initial_state_synced_ = false;
            gateway_session_id_.clear();
            ESP_LOGW("MossGatewayClient", "gateway websocket disconnected");
        });

        {
            std::lock_guard<std::mutex> lock(mutex_);
            websocket_ = socket;
            connected_ = false;
            welcomed_ = false;
            initial_state_synced_ = false;
        }

        ESP_LOGI("MossGatewayClient", "connecting to configured MOSS Gateway");
        if (!socket->Connect(url.c_str())) {
            {
                std::lock_guard<std::mutex> lock(mutex_);
                last_error_ = "gateway websocket connect failed";
                connected_ = false;
                if (websocket_ == socket) {
                    websocket_ = nullptr;
                }
            }
            delete socket;
            finish_connect();
            SetError(error, "gateway websocket connect failed");
            return false;
        }

        {
            std::lock_guard<std::mutex> lock(mutex_);
            connected_ = true;
        }

        if (!SendHello()) {
            SetError(error, "gateway connected but hello send failed");
            DisconnectSocket();
            finish_connect();
            return false;
        }
        finish_connect();
        return true;
    }

    void DisconnectSocket() {
        WebSocket* socket = nullptr;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            socket = websocket_;
            websocket_ = nullptr;
            connected_ = false;
            welcomed_ = false;
            initial_state_synced_ = false;
            gateway_session_id_.clear();
        }
        if (socket) {
            delete socket;
        }
    }

    bool SendHello() {
        auto& adapter = moss::agent::MossAgentAdapter::GetInstance();
#ifdef BOARD_TYPE
        const std::string board_type = BOARD_TYPE;
#else
        const std::string board_type = Board::GetInstance().GetBoardType();
#endif
#ifdef BOARD_NAME
        const std::string board_name = BOARD_NAME;
#else
        const std::string board_name = board_type;
#endif
        std::string hello = adapter.BuildHello(board_type, board_name, true, true, true);
        cJSON* root = cJSON_Parse(hello.c_str());
        if (!root || !cJSON_IsObject(root)) {
            if (root) {
                cJSON_Delete(root);
            }
            return false;
        }
        MossGatewayReadBridge::GetInstance().Advertise(root);
        cJSON_AddStringToObject(root, "device_id", Board::GetInstance().GetUuid().c_str());
        const std::string payload = PrintJson(root);
        cJSON_Delete(root);
        return SendRaw(payload, nullptr);
    }

    bool SendJson(cJSON* root, std::string* error) {
        if (!root) {
            SetError(error, "invalid JSON payload");
            return false;
        }
        return SendRaw(PrintJson(root), error);
    }

    bool SendRaw(const std::string& payload, std::string* error) {
        std::lock_guard<std::recursive_mutex> operation_lock(operation_mutex_);
        WebSocket* socket = nullptr;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (!websocket_ || !connected_ || !websocket_->IsConnected()) {
                SetError(error, "gateway websocket is not connected");
                return false;
            }
            socket = websocket_;
        }

        if (!socket->Send(payload)) {
            std::lock_guard<std::mutex> lock(mutex_);
            last_error_ = "gateway websocket send failed";
            SetError(error, last_error_);
            return false;
        }
        {
            std::lock_guard<std::mutex> lock(mutex_);
            ++messages_sent_;
        }
        return true;
    }

    void OnText(const char* data, size_t len) {
        std::string payload(data, len);
        cJSON* root = cJSON_Parse(payload.c_str());
        if (!root || !cJSON_IsObject(root)) {
            if (root) {
                cJSON_Delete(root);
            }
            std::lock_guard<std::mutex> lock(mutex_);
            last_error_ = "gateway sent invalid JSON";
            return;
        }

        const cJSON* event = cJSON_GetObjectItem(root, "event");
        const std::string event_name = cJSON_IsString(event) ? event->valuestring : "";
        {
            std::lock_guard<std::mutex> lock(mutex_);
            ++messages_received_;
            if (!event_name.empty()) {
                last_event_ = event_name;
            }
            if (event_name == "error") {
                const cJSON* code = cJSON_GetObjectItem(root, "code");
                last_error_ = cJSON_IsString(code) ? code->valuestring : "gateway error";
            }
        }

        if (event_name == "tool_call") {
            std::string current_session;
            {
                std::lock_guard<std::mutex> lock(mutex_);
                current_session = gateway_session_id_;
            }
            MossGatewayReadBridge::GetInstance().HandleToolCall(
                root,
                current_session,
                [this](const std::string& response) {
                    return SendRaw(response, nullptr);
                });
            cJSON_Delete(root);
            return;
        }

        if (event_name == "welcome") {
            const cJSON* gateway_session = cJSON_GetObjectItem(root, "gateway_session_id");
            std::lock_guard<std::mutex> lock(mutex_);
            welcomed_ = true;
            initial_state_synced_ = false;
            if (cJSON_IsString(gateway_session)) {
                gateway_session_id_ = gateway_session->valuestring;
            }
        }
        cJSON_Delete(root);
    }

    bool ConfiguredLocked() const {
        return !url_.empty() && !token_.empty();
    }

    static bool IsValidUrl(const std::string& url) {
        const bool supported_scheme = url.rfind("ws://", 0) == 0 || url.rfind("wss://", 0) == 0;
        if (!supported_scheme) {
            return false;
        }
        const size_t scheme_end = url.find("://");
        if (scheme_end == std::string::npos) {
            return false;
        }
        const size_t authority_start = scheme_end + 3;
        const size_t authority_end = url.find_first_of("/?#", authority_start);
        const std::string authority = url.substr(
            authority_start,
            authority_end == std::string::npos ? std::string::npos : authority_end - authority_start);
        return !authority.empty() && authority.find('@') == std::string::npos;
    }

    static std::string RedactUrl(const std::string& url) {
        const size_t sensitive = url.find_first_of("?#");
        if (sensitive == std::string::npos) {
            return url;
        }
        return url.substr(0, sensitive) + "?[redacted]";
    }

    static void SetError(std::string* error, const std::string& message) {
        if (error) {
            *error = message;
        }
    }

    static std::string PrintJson(cJSON* root) {
        char* raw = cJSON_PrintUnformatted(root);
        std::string result = raw ? raw : "{}";
        if (raw) {
            cJSON_free(raw);
        }
        return result;
    }

    mutable std::mutex mutex_;
    std::recursive_mutex operation_mutex_;
    bool loaded_ = false;
    bool connected_ = false;
    bool welcomed_ = false;
    bool connecting_ = false;
    bool network_ready_ = false;
    bool manual_suspended_ = false;
    bool initial_state_synced_ = false;
    std::string url_;
    std::string token_;
    std::string gateway_session_id_;
    std::string last_event_;
    std::string last_error_;
    uint32_t messages_sent_ = 0;
    uint32_t messages_received_ = 0;
    uint32_t reconnect_attempts_ = 0;
    uint32_t reconnect_failures_ = 0;
    uint32_t heartbeat_failures_ = 0;
    int current_backoff_seconds_ = kInitialReconnectSeconds;
    TaskHandle_t autonomy_task_ = nullptr;
    WebSocket* websocket_ = nullptr;
};

}  // namespace moss::gateway
